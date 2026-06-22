#!/usr/bin/env python3
"""
Coverage-Directed Generation (CDG) proof of concept.

Demonstrates a feedback loop between RTL coverage and test selection.
Three strategies are compared:
  - Random: pick a test type at random each iteration
  - Greedy: always pick the test type with highest expected marginal gain
  - UCB:    Upper Confidence Bound bandit — balance exploitation vs exploration

The "simulation oracle" uses pre-collected per-seed VDB coverage data, so the
loop runs in seconds. To use with a real simulator, replace CoverageOracle with
a class that runs make COV=1 and calls urg to extract the coverage delta.

Usage:
    # Build coverage database from existing VDB first:
    python3 coverage_directed_gen.py --build-db --vdb <path/to/merged.vdb>

    # Then run the CDG loop:
    python3 coverage_directed_gen.py --db /tmp/seed_coverage.json --iters 60

    # Run a specific strategy only:
    python3 coverage_directed_gen.py --db /tmp/seed_coverage.json --strategy ucb
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
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

def run_loop(strategy, oracle, n_iters, seed=42):
    rng = np.random.default_rng(seed)
    current_cov = np.zeros(oracle.n_blocks)
    history = []  # (iter, score, test_type_chosen)

    for i in range(n_iters):
        test_type = strategy.select(current_cov, rng)
        _, new_cov = oracle.run_test(test_type, rng)

        gain = oracle.marginal_gain(current_cov, new_cov)
        current_cov = np.maximum(current_cov, new_cov)
        score = oracle._block_score(current_cov)

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
    parser.add_argument('--build-db', action='store_true',
                        help='Build coverage database from VDB (requires urg)')
    parser.add_argument('--vdb', default=(
        '/opt/ibex/dv/uvm/core_ibex/out/run/coverage/shared_cov/test.vdb'),
        help='Path to merged VDB (for --build-db)')
    parser.add_argument('--db', default='/tmp/seed_coverage.json',
                        help='Path to coverage database JSON')
    parser.add_argument('--iters', type=int, default=60,
                        help='Number of simulated test iterations per strategy')
    parser.add_argument('--strategy', choices=['random', 'greedy', 'ucb', 'all'],
                        default='all', help='Which strategy to run')
    parser.add_argument('--ucb-c', type=float, default=0.5,
                        help='UCB exploration constant (higher = more exploration)')
    parser.add_argument('--seed', type=int, default=42,
                        help='Random seed for reproducibility')
    args = parser.parse_args()

    if args.build_db:
        build_coverage_db(args.vdb, args.db)
        return

    if not os.path.exists(args.db):
        print(f"Coverage database not found: {args.db}")
        print("Run with --build-db first, or point --db at an existing JSON file.")
        sys.exit(1)

    oracle = CoverageOracle(args.db)
    print(f"Loaded coverage oracle: {len(oracle.seeds)} seeds, "
          f"{oracle.n_blocks} branch blocks, {len(oracle.test_types)} test types")
    print(f"Coverage ceiling: {oracle.ceiling_score*100:.2f}%")

    strategies_to_run = []
    if args.strategy in ('random', 'all'):
        strategies_to_run.append(RandomStrategy(oracle))
    if args.strategy in ('greedy', 'all'):
        strategies_to_run.append(GreedyStrategy(oracle))
    if args.strategy in ('ucb', 'all'):
        strategies_to_run.append(UCBStrategy(oracle, c=args.ucb_c))

    histories = []
    for strat in strategies_to_run:
        print(f"\nRunning {strat.name} strategy ({args.iters} iterations)...")
        hist, _ = run_loop(strat, oracle, args.iters, seed=args.seed)
        histories.append((strat.name, hist))
        final = hist[-1][1]
        print(f"  Final coverage: {final*100:.2f}% "
              f"({final/oracle.ceiling_score*100:.1f}% of ceiling)")

    print_results(histories, oracle, args.iters)
    convergence_summary(histories, oracle)


if __name__ == '__main__':
    main()
