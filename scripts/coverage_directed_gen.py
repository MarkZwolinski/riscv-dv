#!/usr/bin/env python3
"""
Coverage-Directed Generation (CDG).

Two backends:

  Oracle mode  (default): uses pre-collected per-seed VDB coverage data as a
                lookup table, so the loop runs in seconds. Good for strategy
                comparison and parameter tuning.

  Real mode (--real):    drives actual VCS simulations via the ibex make flow.
                         run_test() calls `make SIMULATOR=vcs COV=1 TEST=<type>
                         SEED=<seed> GOAL=all`, then extracts per-test coverage
                         from the shared VDB with urg. Build the oracle DB first
                         with --build-db so the real run can reuse the block
                         layout and expected-gain priors.

Three selection strategies:
  Random  — pick a test type uniformly at random
  Greedy  — always pick the type with highest expected marginal gain
  UCB     — Upper Confidence Bound bandit (exploit vs explore); best without a prior

Usage:
    # Build coverage database from an existing regression VDB:
    python3 coverage_directed_gen.py --build-db --vdb <path/to/test.vdb>

    # Run CDG loop in oracle mode (fast, uses pre-collected data):
    python3 coverage_directed_gen.py --db /tmp/seed_coverage.json --iters 60

    # Run CDG loop with REAL VCS simulations (requires ibex build, ~3 min/iter):
    python3 coverage_directed_gen.py --real --db /tmp/seed_coverage.json \\
        --strategy greedy --iters 13

    # Real mode without a prior (UCB explores on its own):
    python3 coverage_directed_gen.py --real --strategy ucb --iters 20
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict

import numpy as np


# ---------------------------------------------------------------------------
# Database builder: extract per-seed coverage from a VDB using urg
# ---------------------------------------------------------------------------

def build_coverage_db(vdb_path, output_path, n_workers=8):
    """Run urg for each seed in the VDB and save a JSON coverage database."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Discover all test seeds
    result = subprocess.run(
        ['urg', '-dir', vdb_path, '-show', 'availabletests'],
        capture_output=True, text=True
    )
    test_dir = os.path.join(os.path.dirname(vdb_path), 'test')
    all_tests = [l.strip() for l in result.stdout.splitlines()
                 if l.strip().startswith(test_dir)]
    print(f"Found {len(all_tests)} test seeds in VDB")

    def extract_one(test_path, tmpdir):
        name = os.path.basename(test_path)
        tl = os.path.join(tmpdir, name + '.list')
        rdir = os.path.join(tmpdir, name)
        with open(tl, 'w') as f:
            f.write(test_path + '\n')
        subprocess.run(
            ['urg', '-dir', vdb_path, '-tests', tl, '-format', 'text',
             '-report', rdir, '-log', '/dev/null'],
            capture_output=True
        )
        modinfo = os.path.join(rdir, 'modinfo.txt')
        if not os.path.exists(modinfo):
            return name, {}
        return name, _parse_branch_blocks(modinfo)

    tmpdir = tempfile.mkdtemp(prefix='/tmp/cdg_db_')
    db = {}
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futures = {ex.submit(extract_one, t, tmpdir): t for t in all_tests}
        done = 0
        for fut in futures:
            name, blocks = fut.result()
            db[name] = blocks
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(all_tests)} done")

    with open(output_path, 'w') as f:
        json.dump(db, f)
    print(f"Saved to {output_path}")
    return db


def _parse_branch_blocks(modinfo_path):
    """Parse modinfo.txt → dict: 'module:TYPE@line' → (covered, total)."""
    blocks = {}
    cur_module = None
    for line in open(modinfo_path):
        m = re.match(r'^Module : (\S+)', line)
        if m:
            cur_module = m.group(1)
            continue
        if cur_module:
            bm = re.match(
                r'(CASE|IF|TERNARY)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)', line
            )
            if bm:
                key = f"{cur_module}:{bm.group(1)}@{bm.group(2)}"
                blocks[key] = (int(bm.group(4)), int(bm.group(3)))
    return blocks


# ---------------------------------------------------------------------------
# Coverage oracle: wraps the seed database
# ---------------------------------------------------------------------------

class CoverageOracle:
    """
    Simulation oracle backed by pre-collected per-seed coverage data.

    In a real system, replace run_test() with a call to the VCS simulator
    and urg, e.g.:
        make SIMULATOR=vcs ISS=spike IBEX_CONFIG=small COV=1 \
             TEST=<test_type> SEED=<seed> GOAL=run
        urg -dir out/run/coverage/test.vdb -tests <test_list> ...
    """

    def __init__(self, db_path):
        raw = json.load(open(db_path))
        self.seeds = sorted(raw.keys())
        self.all_blocks = sorted({k for v in raw.values() for k in v})
        self.n_blocks = len(self.all_blocks)
        self.block_idx = {b: i for i, b in enumerate(self.all_blocks)}

        # M_cov[seed_idx, block_idx] = covered branches (int)
        # M_tot[block_idx] = total branches in block
        self.M_cov = np.zeros((len(self.seeds), self.n_blocks), dtype=np.float32)
        self.M_tot = np.zeros(self.n_blocks, dtype=np.float32)
        for i, s in enumerate(self.seeds):
            for blk, (cov, tot) in raw[s].items():
                j = self.block_idx[blk]
                self.M_cov[i, j] = cov
                self.M_tot[j] = tot

        # Group seed indices by test type
        self.by_type = defaultdict(list)
        for i, s in enumerate(self.seeds):
            m = re.match(r'test_(riscv_\w+_test)_\d+', s)
            if m:
                self.by_type[m.group(1)].append(i)
        self.test_types = sorted(self.by_type.keys())

        # Ceiling: max achievable per block across ALL seeds in database
        self.ceiling = self.M_cov.max(axis=0)
        self.ceiling_score = self._block_score(self.ceiling)

    def _block_score(self, cov_vec):
        """Mean fractional coverage across blocks (weighted by block size)."""
        denom = np.where(self.M_tot > 0, self.M_tot, 1.0)
        return float((cov_vec / denom).mean())

    def run_test(self, test_type, rng):
        """
        Simulate running one seed of test_type.
        Returns (seed_index, coverage_vector).
        Replace this method with a real simulator call for production use.
        """
        candidates = self.by_type[test_type]
        seed_idx = rng.choice(candidates)
        return seed_idx, self.M_cov[seed_idx]

    def marginal_gain(self, current_cov, new_cov_vec):
        """Coverage increase from adding new_cov_vec to current state."""
        merged = np.maximum(current_cov, new_cov_vec)
        return self._block_score(merged) - self._block_score(current_cov)

    def expected_gain_per_type(self, current_cov):
        """Mean expected marginal gain for each test type (over all its seeds)."""
        gains = {}
        for tt in self.test_types:
            g = np.mean([
                self.marginal_gain(current_cov, self.M_cov[i])
                for i in self.by_type[tt]
            ])
            gains[tt] = g
        return gains


# ---------------------------------------------------------------------------
# Real simulator oracle: drives VCS via the ibex make flow
# ---------------------------------------------------------------------------

class RealSimOracle:
    """
    Coverage oracle that runs actual VCS simulations via the ibex make flow.

    Each run_test() call:
      1. Picks a fresh seed (never in the pre-existing VDB).
      2. Runs: make SIMULATOR=vcs ISS=spike IBEX_CONFIG=small COV=1 GOAL=all
                   TEST=<type> SEED=<seed> ITERATIONS=1
         Coverage is appended to the shared VDB with -cm_name test_<type>_<seed>.
      3. Extracts per-test branch coverage from that VDB entry using urg.
      4. Returns (seed, coverage_vector).

    For expected_gain_per_type(), delegates to a CoverageOracle prior when one
    is provided (historical data gives accurate gain estimates without running
    simulations). Without a prior, falls back to running means of observed gains.

    Arguments
    ---------
    ibex_dir       Path to /opt/ibex/dv/uvm/core_ibex (where Makefile lives).
    riscvdv_root   RISCV_DV_ROOT exported to make (path to this repo).
    pkg_config_path  PKG_CONFIG_PATH for spike-lowrisc; empty string to inherit.
    all_blocks, block_idx, M_tot, test_types
                   Coverage schema shared with the strategy classes. Typically
                   copied from a CoverageOracle loaded from --db.
    prior          Optional CoverageOracle used only for expected_gain_per_type().
    """

    _DEFAULT_IBEX   = '/opt/ibex/dv/uvm/core_ibex'
    _DEFAULT_DV     = '/home/mz1/riscv-dv'
    _DEFAULT_PKG    = '/opt/spike-lowrisc/install/lib/pkgconfig'
    _CDG_OUT        = 'out_cdg'   # dedicated output dir — avoids polluting regression out/
    _SEED_START     = 2_000_000   # fresh seeds stay above existing regression range
    _TIMEOUT_S      = 600         # hard per-test wall-clock cap

    def __init__(self, ibex_dir, riscvdv_root, pkg_config_path,
                 all_blocks, block_idx, M_tot, test_types, prior=None):
        self.ibex_dir        = ibex_dir
        self.riscvdv_root    = riscvdv_root
        self.pkg_config_path = pkg_config_path
        self.all_blocks      = all_blocks
        self.n_blocks        = len(all_blocks)
        self.block_idx       = block_idx
        self.M_tot           = M_tot
        self.test_types      = test_types
        self.prior           = prior

        # For print_results / convergence_summary compatibility
        self.seeds = []
        if prior is not None:
            self.ceiling       = prior.ceiling
            self.ceiling_score = prior.ceiling_score
        else:
            self.ceiling       = np.zeros(self.n_blocks)
            self.ceiling_score = 0.0

        # CDG uses its own output directory to avoid clobbering the regression
        # metadata and to keep the VDB separate.  The pre-built TB and instr-gen
        # binaries are symlinked in so we don't pay the ~10 min recompile cost.
        self._setup_cdg_outdir()

        self.vdb_path   = os.path.join(ibex_dir, self._CDG_OUT,
                                       'run/coverage/shared_cov/test.vdb')
        self.type_obs   = defaultdict(list)    # observed cov vectors per type
        self._seed_iter = self._fresh_seeds()

    @staticmethod
    def _fix_vcs_archive_symlinks(daidir):
        """Create compatibility symlinks for VCS archive .so files renamed by recompile.

        VCS incremental recompile workflow:
          - The OLD delta archive (_<oldhash>_archive_1.so) is renamed to
            _prev_archive_1.so (the "base" archive keeps its name).
          - A NEW delta archive (_<newhash>_archive_1.so) is created.
          - The main binary's DT_NEEDED still references the OLD hash name.

        After a recompile, the binary needs _<oldhash>_archive_1.so which no
        longer exists.  Alias it to the NEW delta (_<newhash>_archive_1.so),
        which contains the same (or updated) symbols.

        If no recompile has happened, _prev_archive_1.so doesn't exist and
        there is nothing to fix.
        """
        import glob
        prev = os.path.join(daidir, '_prev_archive_1.so')
        if not os.path.exists(prev):
            return  # no incremental recompile happened — nothing to fix

        # All named archives in the daidir (excluding _prev_archive_1.so).
        # After a recompile there is typically ONE: the new delta.
        named = {
            os.path.basename(p)
            for p in glob.glob(os.path.join(daidir, '_*_archive_1.so'))
            if os.path.basename(p) != '_prev_archive_1.so'
        }
        if not named:
            return  # nothing to alias to

        # Pick the most recently modified archive as the alias target.
        newest = max(
            named,
            key=lambda n: os.path.getmtime(os.path.join(daidir, n))
        )

        parent_bin = os.path.join(os.path.dirname(daidir), 'vcs_simv')
        if not os.path.exists(parent_bin):
            return
        try:
            out = subprocess.run(
                ['strings', parent_bin],
                capture_output=True, text=True).stdout
        except FileNotFoundError:
            return

        for line in out.splitlines():
            if (line.endswith('_archive_1.so')
                    and line not in named
                    and line != '_prev_archive_1.so'):
                target = os.path.join(daidir, line)
                if not os.path.exists(target):
                    os.symlink(newest, target)

    def _setup_cdg_outdir(self):
        """
        Prepare out_cdg/ so make skips recompilation and runs only the sim.

        The ibex build system checks two things before deciding to rebuild:
          1. A stamp file (metadata/<step>.stamp) — must exist and be newer
             than all source deps.
          2. A vars.mk file (build/.<step>.vars.mk) — records which make
             variables were active; a mismatch forces a clean rebuild.

        We satisfy both by copying the existing binaries and vars files from
        out/build/ into out_cdg/build/.  Copying (not symlinking) the
        directories avoids the "Cannot rmtree a symlink" error that
        build_instr_gen.py raises when it tries to clean a symlinked dir.
        """
        import shutil

        src_build  = os.path.join(self.ibex_dir, 'out/build')
        src_meta   = os.path.join(self.ibex_dir, 'out/metadata')
        out        = os.path.join(self.ibex_dir, self._CDG_OUT)
        dst_build  = os.path.join(out, 'build')
        dst_meta   = os.path.join(out, 'metadata')

        os.makedirs(dst_meta, exist_ok=True)
        os.makedirs(dst_build, exist_ok=True)

        # ---- copy compiled build trees (tb/ and instr_gen/) ----
        # We copy files and symlink subdirectories.  Copying the top-level dir
        # avoids the "Cannot rmtree a symlink" error that build_instr_gen.py
        # raises if the whole dir is a symlink.  The subdirs (e.g. vcs_simv.csrc/
        # containing VCS shared libs, and asm_test/) are read-only at runtime so
        # symlinking them is safe.
        # tb/ is intentionally NOT copied — the TB binary and its
        # vcs_simv.daidir/ must be compiled fresh together to guarantee
        # consistency (incremental VCS recompiles can update the daidir
        # without relinking the binary, leaving them incompatible).
        # make compiles the TB once in out_cdg/build/tb/ on the first CDG run
        # and reuses it for all subsequent iterations.
        for subdir in ('instr_gen',):
            dst_dir = os.path.join(dst_build, subdir)
            src_dir = os.path.join(src_build, subdir)
            if not os.path.exists(dst_dir) and os.path.isdir(src_dir):
                os.makedirs(dst_dir, exist_ok=True)
                for fname in os.listdir(src_dir):
                    src_f = os.path.join(src_dir, fname)
                    dst_f = os.path.join(dst_dir, fname)
                    if os.path.exists(dst_f):
                        continue
                    if os.path.isfile(src_f):
                        shutil.copy2(src_f, dst_f)
                    elif os.path.isdir(src_f):
                        if fname.endswith('.daidir'):
                            # VCS simulation archives live in .daidir/.  Copy
                            # the whole dir; symlinks are added afterward (after
                            # the loop) because the parent vcs_simv binary must
                            # already exist to determine which archive names to
                            # alias.
                            shutil.copytree(src_f, dst_f)
                        else:
                            # Other subdirs (vcs_simv.csrc/, asm_test/) are safe
                            # to symlink.
                            os.symlink(src_f, dst_f)

        # ---- copy vars.mk files so make sees the same build config ----
        for fname in ('.tb.vars.mk', '.instr_gen.vars.mk', '.cc.vars.mk'):
            src_f = os.path.join(src_build, fname)
            dst_f = os.path.join(dst_build, fname)
            if os.path.isfile(src_f) and not os.path.exists(dst_f):
                shutil.copy2(src_f, dst_f)

        # ---- copy build stamps and advance their timestamps ----
        # make considers a target stale if any prereq is >= as new as the target.
        # We touch the stamps to be 5 seconds in the future so they are always
        # strictly newer than the vars.mk files we just copied.
        #
        # NOTE: tb.compile.stamp is intentionally NOT copied here.  The TB
        # binary and its elaboration database (vcs_simv.daidir/) can be left
        # inconsistent by a VCS incremental recompile.  Letting make compile
        # the TB fresh in out_cdg/ costs ~10 min on the first CDG run but
        # guarantees binary/daidir consistency and a correct VDB.
        future = time.time() + 5
        for stamp in ('instr.gen.build.stamp', 'core.config.stamp'):
            src_f = os.path.join(src_meta, stamp)
            dst_f = os.path.join(dst_meta, stamp)
            if not os.path.exists(dst_f):
                if os.path.isfile(src_f):
                    shutil.copy2(src_f, dst_f)
                else:
                    open(dst_f, 'w').close()
                os.utime(dst_f, (future, future))

        # ---- copy riscv_core_setting.sv produced by core_config step ----
        cc_src = os.path.join(self.ibex_dir, 'riscv_dv_extension/riscv_core_setting.sv')
        if os.path.isfile(cc_src):
            # Already in-place (shared between out/ and out_cdg/ runs)
            pass

    # ------------------------------------------------------------------
    # Seed management

    def _fresh_seeds(self):
        """Yield integer seeds that are not already in the VDB."""
        existing = self._existing_seeds_in_vdb()
        seed = self._SEED_START
        while True:
            if seed not in existing:
                yield seed
            seed += 1

    def _existing_seeds_in_vdb(self):
        # Check the CDG VDB (out_cdg); the regression VDB (out/) is separate
        if not os.path.exists(self.vdb_path):
            return set()
        out = subprocess.run(
            ['urg', '-dir', self.vdb_path, '-show', 'availabletests'],
            capture_output=True, text=True).stdout
        seeds = set()
        for line in out.splitlines():
            m = re.search(r'_(\d+)\s*$', line.strip())
            if m:
                seeds.add(int(m.group(1)))
        return seeds

    # ------------------------------------------------------------------
    # Core oracle interface

    def _block_score(self, cov_vec):
        denom = np.where(self.M_tot > 0, self.M_tot, 1.0)
        return float((cov_vec / denom).mean())

    def marginal_gain(self, current_cov, new_cov_vec):
        merged = np.maximum(current_cov, new_cov_vec)
        return self._block_score(merged) - self._block_score(current_cov)

    def _reset_metadata(self):
        """Delete per-run metadata files so make regenerates them for the next test.

        Keeps the build stamps (tb.compile.stamp, instr.gen.build.stamp,
        core.config.stamp) to avoid recompiling the TB/instr_gen.  Deletes
        everything else: metadata.yaml, metadata.pickle, per-test pickles, and
        post-sim stamps (merge.cov.stamp, regr.log.stamp, fcov.stamp).
        """
        keep = {'tb.compile.stamp', 'instr.gen.build.stamp', 'core.config.stamp',
                'fcov.stamp'}
        meta_dir = os.path.join(self.ibex_dir, self._CDG_OUT, 'metadata')
        if not os.path.isdir(meta_dir):
            return
        for fname in os.listdir(meta_dir):
            if fname not in keep:
                try:
                    os.remove(os.path.join(meta_dir, fname))
                except OSError:
                    pass

    def run_test(self, test_type, rng):
        """Run one seed of test_type through VCS and return (seed, cov_vec)."""
        import shutil
        seed = next(self._seed_iter)
        self.seeds.append(f"test_{test_type}_{seed}")

        # Remove the test run directory if it exists from a prior attempt.  Make
        # treats an existing directory as evidence that gcc_compile already ran and
        # skips it, leaving the binary path pointing at a now-deleted /tmp dir.
        test_run_dir = os.path.join(self.ibex_dir, self._CDG_OUT,
                                    'run/tests', f'{test_type}.{seed}')
        if os.path.isdir(test_run_dir):
            shutil.rmtree(test_run_dir)

        # Remove stale metadata so make regenerates create_metadata for this test/seed.
        self._reset_metadata()

        env = {**os.environ, 'RISCV_DV_ROOT': self.riscvdv_root}
        if self.pkg_config_path:
            env['PKG_CONFIG_PATH'] = self.pkg_config_path

        print(f"  [{test_type}] seed={seed}  running VCS...")
        t0 = time.time()
        logfile = os.path.join(self.ibex_dir, self._CDG_OUT,
                               f'cdg_{test_type}_{seed}.log')
        try:
            proc = subprocess.run(
                ['make', '--no-print-directory',
                 f'OUT={self._CDG_OUT}',          # isolated output dir
                 'SIMULATOR=vcs', 'ISS=spike', 'IBEX_CONFIG=small',
                 'COV=1', 'GOAL=check_logs',      # skip fcov/merge_cov/collect_results
                 f'TEST={test_type}', f'SEED={seed}', 'ITERATIONS=1'],
                cwd=self.ibex_dir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=self._TIMEOUT_S)
            elapsed = time.time() - t0
            ok = proc.returncode == 0
            print(f"  [{test_type}] {'PASS' if ok else 'FAIL'} in {elapsed:.0f}s")
            # Always save the log for post-mortem inspection
            with open(logfile, 'w') as f:
                f.write(proc.stdout)
            if not ok:
                lines = proc.stdout.splitlines()
                for ln in lines[-20:]:
                    print(f"    {ln}")
        except subprocess.TimeoutExpired:
            print(f"  [{test_type}] TIMEOUT after {self._TIMEOUT_S}s")
            return seed, np.zeros(self.n_blocks)

        cov_vec = self._extract_coverage(test_type, seed)
        self.type_obs[test_type].append(cov_vec)
        return seed, cov_vec

    def _extract_coverage(self, test_type, seed):
        """Extract per-test branch coverage from the VDB for test_type/seed."""
        test_name = f"test_{test_type}_{seed}"

        # Locate the test's data path inside the VDB
        avail = subprocess.run(
            ['urg', '-dir', self.vdb_path, '-show', 'availabletests'],
            capture_output=True, text=True).stdout
        test_path = next(
            (ln.strip() for ln in avail.splitlines()
             if os.path.basename(ln.strip()) == test_name),
            None)
        if test_path is None:
            print(f"  WARNING: {test_name} not in VDB — cosim failure or "
                  "coverage not written")
            return np.zeros(self.n_blocks)

        with tempfile.TemporaryDirectory() as td:
            tl   = os.path.join(td, 'tests.list')
            rdir = os.path.join(td, 'report')
            with open(tl, 'w') as f:
                f.write(test_path + '\n')
            subprocess.run(
                ['urg', '-dir', self.vdb_path, '-tests', tl,
                 '-format', 'text', '-report', rdir, '-log', '/dev/null'],
                capture_output=True)
            modinfo = os.path.join(rdir, 'modinfo.txt')
            if not os.path.exists(modinfo):
                return np.zeros(self.n_blocks)
            blocks = _parse_branch_blocks(modinfo)

        vec = np.zeros(self.n_blocks)
        for blk, (cov, _) in blocks.items():
            if blk in self.block_idx:
                vec[self.block_idx[blk]] = cov
        return vec

    def expected_gain_per_type(self, current_cov):
        """
        Estimate expected marginal gain per test type.

        With a prior: delegate to CoverageOracle (uses all 480 historical seeds —
        very accurate, no simulations needed for estimation).

        Without a prior: use running means from real simulations so far.
        Test types not yet run get an optimistic estimate so the bandit explores
        them before committing.
        """
        if self.prior is not None:
            return self.prior.expected_gain_per_type(current_cov)

        tried_means = [
            np.mean([self.marginal_gain(current_cov, o) for o in obs])
            for obs in self.type_obs.values() if obs
        ]
        default = (np.mean(tried_means) * 2.0) if tried_means else 0.01

        gains = {}
        for tt in self.test_types:
            if self.type_obs[tt]:
                gains[tt] = np.mean([
                    self.marginal_gain(current_cov, o)
                    for o in self.type_obs[tt]
                ])
            else:
                gains[tt] = default
        return gains


# ---------------------------------------------------------------------------
# Selection strategies
# ---------------------------------------------------------------------------

class RandomStrategy:
    name = 'Random'

    def __init__(self, oracle):
        self.oracle = oracle

    def select(self, current_cov, rng):
        return rng.choice(self.oracle.test_types)

    def update(self, test_type, gain):
        pass


class GreedyStrategy:
    """Always pick the test type with the highest expected marginal gain."""
    name = 'Greedy'

    def __init__(self, oracle):
        self.oracle = oracle

    def select(self, current_cov, rng):
        gains = self.oracle.expected_gain_per_type(current_cov)
        return max(gains, key=gains.get)

    def update(self, test_type, gain):
        pass


class UCBStrategy:
    """
    UCB-1 bandit over test types.
    Balances exploitation (known high-gain types) with exploration (less-tried types).
    c controls the exploration bonus; higher c = more exploration.
    """
    name = 'UCB'

    def __init__(self, oracle, c=0.5):
        self.oracle = oracle
        self.c = c
        self.counts = {tt: 0 for tt in oracle.test_types}
        self.values = {tt: 0.0 for tt in oracle.test_types}
        self.total = 0
        self._uninit = list(oracle.test_types)  # not yet tried

    def select(self, current_cov, rng):
        # Initialise: try each type once before using UCB
        if self._uninit:
            tt = self._uninit.pop(0)
            return tt
        ucb = {
            tt: self.values[tt] + self.c * np.sqrt(np.log(self.total) / self.counts[tt])
            for tt in self.oracle.test_types
        }
        return max(ucb, key=ucb.get)

    def update(self, test_type, gain):
        self.counts[test_type] += 1
        self.total += 1
        # Incremental running average
        self.values[test_type] += (gain - self.values[test_type]) / self.counts[test_type]


# ---------------------------------------------------------------------------
# Main simulation loop
# ---------------------------------------------------------------------------

def run_loop(strategy, sim, n_iters, seed=42):
    """
    Run n_iters iterations of the CDG loop.

    sim can be a CoverageOracle (lookup-table mode) or a RealSimOracle
    (actual VCS simulation mode) — both expose the same interface.
    """
    rng = np.random.default_rng(seed)
    current_cov = np.zeros(sim.n_blocks)
    history = []  # (iter, score, test_type_chosen, gain)

    for i in range(n_iters):
        test_type = strategy.select(current_cov, rng)
        _, new_cov = sim.run_test(test_type, rng)

        gain = sim.marginal_gain(current_cov, new_cov)
        current_cov = np.maximum(current_cov, new_cov)
        score = sim._block_score(current_cov)

        strategy.update(test_type, gain)
        history.append((i + 1, score, test_type, gain))

    return history, current_cov


def print_results(histories, oracle, n_iters):
    print(f"\n{'='*72}")
    print(f"Coverage-Directed Generation — Simulation Results")
    print(f"Oracle: {len(oracle.seeds)} seeds, {oracle.n_blocks} branch blocks")
    print(f"Coverage ceiling (best possible from existing seeds): "
          f"{oracle.ceiling_score*100:.2f}%")
    print(f"{'='*72}\n")

    # Progress table: show score at 1, 5, 10, 20, 40, n_iters
    checkpoints = sorted({1, 5, 10, 20, 40, n_iters})
    checkpoints = [c for c in checkpoints if c <= n_iters]

    header = f"{'Iter':>5}" + "".join(f"  {name:>10}" for name, _ in histories)
    print(header)
    print('-' * len(header))

    for cp in checkpoints:
        row = f"{cp:>5}"
        for name, hist in histories:
            score = next(s for i, s, *_ in hist if i == cp)
            row += f"  {score*100:>9.2f}%"
        print(row)

    print(f"\n{'='*72}")
    print("Final scores vs ceiling:")
    for name, hist in histories:
        final = hist[-1][1]
        gap = oracle.ceiling_score - final
        pct_closed = (final / oracle.ceiling_score) * 100 if oracle.ceiling_score > 0 else 0
        print(f"  {name:<10}  {final*100:.2f}%  "
              f"(gap to ceiling: {gap*100:.3f}%,  {pct_closed:.1f}% of ceiling reached)")

    # Coverage gap analysis: which blocks remain hardest?
    print(f"\n{'='*72}")
    print("Hardest blocks (lowest ceiling coverage — need new test types):\n")
    ceiling_frac = oracle.ceiling / np.where(oracle.M_tot > 0, oracle.M_tot, 1.0)
    hard_idx = np.argsort(ceiling_frac)[:10]
    print(f"  {'Block':<55}  {'Ceiling':>8}  {'Note'}")
    print(f"  {'-'*80}")
    for idx in hard_idx:
        blk = oracle.all_blocks[idx]
        cf = ceiling_frac[idx]
        note = "structural gap — directed test needed" if cf < 0.5 else ""
        print(f"  {blk:<55}  {cf*100:>7.1f}%  {note}")

    # Test type selection frequency for UCB
    print(f"\n{'='*72}")
    for name, hist in histories:
        type_counts = defaultdict(int)
        for _, _, tt, _ in hist:
            type_counts[tt] += 1
        top = sorted(type_counts.items(), key=lambda x: -x[1])[:8]
        print(f"{name} — most selected test types:")
        for tt, cnt in top:
            short = tt.replace('riscv_', '').replace('_test', '')
            print(f"  {short:<42}  {cnt:>3}x")
        print()


def convergence_summary(histories, oracle):
    """How many iterations to reach 90%, 95%, 99% of ceiling."""
    targets = [0.90, 0.95, 0.99]
    print(f"\n{'='*72}")
    print("Iterations to reach fraction of coverage ceiling:\n")
    header = f"  {'Target':>8}" + "".join(f"  {name:>10}" for name, _ in histories)
    print(header)
    print(f"  {'-'*(len(header)-2)}")
    for t in targets:
        threshold = oracle.ceiling_score * t
        row = f"  {t*100:>7.0f}%"
        for name, hist in histories:
            reached = next((i for i, s, *_ in hist if s >= threshold), None)
            row += f"  {str(reached) if reached else '>'+str(len(hist)):>10}"
        print(row)
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)

    # ---- data / oracle ----
    parser.add_argument('--build-db', action='store_true',
                        help='Build coverage database from VDB (requires urg)')
    parser.add_argument('--vdb',
                        default='/opt/ibex/dv/uvm/core_ibex/out/run/coverage/'
                                'shared_cov/test.vdb',
                        help='Path to shared_cov VDB (for --build-db)')
    parser.add_argument('--db', default='/tmp/seed_coverage.json',
                        help='Path to coverage database JSON')

    # ---- real simulator ----
    parser.add_argument('--real', action='store_true',
                        help='Drive actual VCS simulations instead of oracle lookup')
    parser.add_argument('--ibex-dir',
                        default=RealSimOracle._DEFAULT_IBEX,
                        help='Path to ibex dv directory (contains Makefile)')
    parser.add_argument('--riscvdv-root',
                        default=RealSimOracle._DEFAULT_DV,
                        help='RISCV_DV_ROOT exported to make')
    parser.add_argument('--pkg-config-path',
                        default=RealSimOracle._DEFAULT_PKG,
                        help='PKG_CONFIG_PATH for spike libs; empty to inherit')

    # ---- loop control ----
    parser.add_argument('--iters', type=int, default=60,
                        help='CDG iterations per strategy')
    parser.add_argument('--strategy', choices=['random', 'greedy', 'ucb', 'all'],
                        default='all', help='Which strategy to run')
    parser.add_argument('--ucb-c', type=float, default=0.5,
                        help='UCB exploration constant (higher = more exploration)')
    parser.add_argument('--seed', type=int, default=42,
                        help='Random seed for reproducibility')

    args = parser.parse_args()

    # ------------------------------------------------------------------ build
    if args.build_db:
        build_coverage_db(args.vdb, args.db)
        return

    # ------------------------------------------------------------------ setup
    if args.real:
        # Real mode: must have a block schema (from --db) OR discover from VDB.
        # We always prefer loading --db as a prior so Greedy can use historical
        # expected-gain estimates without running extra simulations.
        if not os.path.exists(args.db):
            print(f"ERROR: --real requires --db for block layout and test types.\n"
                  f"Run --build-db first: "
                  f"python3 coverage_directed_gen.py --build-db --vdb {args.vdb}")
            sys.exit(1)

        prior = CoverageOracle(args.db)
        sim = RealSimOracle(
            ibex_dir        = args.ibex_dir,
            riscvdv_root    = args.riscvdv_root,
            pkg_config_path = args.pkg_config_path,
            all_blocks      = prior.all_blocks,
            block_idx       = prior.block_idx,
            M_tot           = prior.M_tot,
            test_types      = prior.test_types,
            prior           = prior,
        )
        print(f"Real-sim mode: ibex={args.ibex_dir}")
        print(f"  RISCV_DV_ROOT={args.riscvdv_root}")
        print(f"  Prior oracle: {len(prior.seeds)} seeds, "
              f"{prior.n_blocks} blocks, {len(prior.test_types)} test types")
        print(f"  Coverage ceiling (from prior): {prior.ceiling_score*100:.2f}%")
        print(f"  VDB: {sim.vdb_path}")
        print(f"  Existing seeds excluded: {len(sim._existing_seeds_in_vdb())}")
    else:
        if not os.path.exists(args.db):
            print(f"Coverage database not found: {args.db}")
            print("Run with --build-db first, or point --db at an existing file.")
            sys.exit(1)
        prior = None
        sim = CoverageOracle(args.db)
        print(f"Oracle mode: {len(sim.seeds)} seeds, "
              f"{sim.n_blocks} branch blocks, {len(sim.test_types)} test types")
        print(f"Coverage ceiling: {sim.ceiling_score*100:.2f}%")

    # ------------------------------------------------------------------ run
    strategies_to_run = []
    if args.strategy in ('random', 'all'):
        strategies_to_run.append(RandomStrategy(sim))
    if args.strategy in ('greedy', 'all'):
        strategies_to_run.append(GreedyStrategy(sim))
    if args.strategy in ('ucb', 'all'):
        strategies_to_run.append(UCBStrategy(sim, c=args.ucb_c))

    histories = []
    for strat in strategies_to_run:
        if args.real:
            # Re-create sim so each strategy starts from the same VDB state.
            # Share the same prior but each strategy writes to a separate VDB
            # accumulation by using fresh seeds.
            sim_s = RealSimOracle(
                ibex_dir        = args.ibex_dir,
                riscvdv_root    = args.riscvdv_root,
                pkg_config_path = args.pkg_config_path,
                all_blocks      = prior.all_blocks,
                block_idx       = prior.block_idx,
                M_tot           = prior.M_tot,
                test_types      = prior.test_types,
                prior           = prior,
            )
            strat_instance = type(strat)(sim_s) if not isinstance(strat, UCBStrategy) \
                             else UCBStrategy(sim_s, c=args.ucb_c)
        else:
            sim_s = sim
            strat_instance = strat

        print(f"\nRunning {strat.name} strategy ({args.iters} iterations)...")
        hist, _ = run_loop(strat_instance, sim_s, args.iters, seed=args.seed)
        histories.append((strat.name, hist))
        final = hist[-1][1]
        ceil = sim_s.ceiling_score
        pct = (final / ceil * 100) if ceil > 0 else 0.0
        print(f"  Final coverage: {final*100:.2f}%"
              + (f" ({pct:.1f}% of ceiling)" if ceil > 0 else ""))

    # In real mode, use the prior (CoverageOracle) for display metadata
    # (seed count, ceiling). The strategies still report real-sim coverage.
    display_oracle = prior if (args.real and prior is not None) else sim
    print_results(histories, display_oracle, args.iters)
    convergence_summary(histories, display_oracle)


if __name__ == '__main__':
    main()
