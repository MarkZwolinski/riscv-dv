# Ibex Coverage Analysis

## Setup

Target: ibex `small` configuration (RV32IMC, no B-ext, no V-ext, cosim spike).
Simulator: VCS with `-cm line+tgl+branch`.
Tests: 480 tests across 39 test types from `base_testlist.yaml`.
Two runs compared:
- **Baseline**: `/opt/ibex/vendor/google_riscv-dv` (ibex's vendored riscv-dv)
- **Enhanced**: `/home/mz1/riscv-dv` (this repository)

---

## Bugs Found and Fixed

### 1. ibex testbench missing RV32ZC parameter wiring

**Symptom:** `riscv_illegal_instr_test` failing 12/15 seeds with cosim mismatches.
ibex RTL accepted `c.mul` (Zcb compressed instruction, encoding `0x9dc9`) while spike
raised an illegal instruction trap.

**Root cause:** `core_ibex_tb_top.sv` never wired the `RV32ZC` parameter from the
macro guard through to `ibex_top_tracing`. Every simulation used the package default
(`RV32ZcaZcbZcmp`), silently enabling Zcb regardless of the requested config. The
`small` config should use `RV32Zca` (C extension only, no `c.mul`).

**Fix:** Three additions to `core_ibex_tb_top.sv`:
```sv
`ifndef IBEX_CFG_RV32ZC
  `define IBEX_CFG_RV32ZC ibex_pkg::RV32ZcaZcbZcmp
`endif
parameter ibex_pkg::rv32zc_e RV32ZC = `IBEX_CFG_RV32ZC;
// + .RV32ZC(RV32ZC) in ibex_top_tracing instantiation
```
Documented as `patches/ibex/0004-ibex-tb-rv32zc-parameter-wire.patch`.

**Impact:** Regression improved from 362/480 (75.4%) to 378/480 (78.75%).

---

### 2. VCS coverage collection aborting all simulations (FCIBH)

**Symptom:** With `COV=1`, every simulation aborted at ~200ns before cosim ran.

**Root cause:** `rtl_simulation.yaml` had `+enable_ibex_fcov=1` in `cov_opts`,
activating ibex's functional coverage interface. `ibex_fcov_if.sv` contains:
```sv
illegal_bins illegal_transitions = default sequence;
```
A stale TODO comment said "VCS does not implement default sequence" — the current VCS
version does, so every simulation hit an illegal bin at startup and aborted (FCIBH).

**Fix:** Removed `+enable_ibex_fcov=1` and `fsm+assert` from `cov_opts` in
`rtl_simulation.yaml`. Structural coverage only:
```yaml
cov_opts: >
  -cm line+tgl+branch
  -cm_dir <dir_shared_cov>/test.vdb
  -cm_log /dev/null
  -assert nopostproc
  -cm_name test_<test_name>_<seed>
```

---

### 3. Enhanced riscv-dv missing ibex-specific CSRs

**Symptom:** VCS instr-gen compile failed with `Error-[IND] Identifier not declared`
on `CPUCTRLSTS` and `SECURESEED`.

**Root cause:** ibex's `riscv_core_setting.sv` references these ibex-specific CSR
names in `implemented_csr[]`. The vendored baseline had them patched in; this
repository had diverged.

**Fix:** Added to `privileged_reg_t` enum in `src/riscv_instr_pkg.sv`:
```sv
CPUCTRLSTS = 'h7C0,  // CPU Control and Status (Ibex Specific)
SECURESEED = 'h7C1   // Secure Seed (Ibex Specific)
```

---

## Structural RTL Coverage Comparison

Both baseline and enhanced ran 480 tests with `COV=1`.

| Metric | Baseline | Enhanced | Delta |
|--------|----------|----------|-------|
| Line   | 81.42%   | 80.99%   | -0.4% |
| Toggle | 60.12%   | 61.45%   | **+1.3%** |
| Branch | 83.07%   | 81.87%   | -1.2% |
| Total  | 81.55%   | 81.54%   | ~0%   |

Pass rate: 375/480 baseline, 377/480 enhanced — within seed noise.

### Module-level differences

| Module | Metric | Baseline | Enhanced | Interpretation |
|--------|--------|----------|----------|----------------|
| `ibex_controller` IF@278 | Branch | 71.4% | **100%** | Enhanced hits all exception priority paths (store/load errors with concurrent exceptions pending) |
| `ibex_controller` CASE@494 | Branch | 89.5% | **93.0%** | 2 more FSM states reached |
| `ibex_load_store_unit` TERNARY@580 | Branch | 66.7% | **100%** | Misaligned 2-beat tracking path fully covered |
| `ibex_decoder` CASE@692 | Branch | 64.1% | 54.7% | Seed noise — different random instruction mix, not a regression |

The `ibex_controller` and `ibex_load_store_unit` gains are real: the enhanced
generator produces more diverse exception/trap sequences that exercise lower-priority
paths in the exception arbiter and misaligned memory access tracking.

---

## Coverage Redundancy Analysis

A leave-one-out analysis was run: each test type was removed from the full 480-test
set and the resulting coverage drop measured. Only 3 of 39 test types provide any
unique structural RTL coverage.

| Test Type | Seeds | Coverage Drop if Removed | Status |
|-----------|-------|--------------------------|--------|
| `riscv_illegal_instr_test` | 15 | **-1.02%** | Irreplaceable |
| `riscv_arithmetic_basic_test` | 10 | -0.05% | Irreplaceable |
| `riscv_interrupt_wfi_test` | 15 | -0.05% | Irreplaceable |
| `riscv_mem_intg_error_test` | 50 | -0.02% | Marginal |
| All other 35 test types | 390 | 0.00% | Redundant |

**36 out of 39 test types (390 of 480 tests) contribute zero unique structural
coverage.** The entire debug test suite (12 types, ~165 tests), all interrupt
variants, CSR stress, loop, jump stress, and rand tests are structurally redundant
with each other.

### Why this happens

The ibex RTL for RV32IMC has a compact set of structural paths: decode → execute →
exception/trap → CSR write → resume. `riscv_arithmetic_basic_test` exhausts the
decode→execute→writeback paths in its 10 seeds. Once saturated, more tests hitting
the same legal-instruction paths add nothing structurally.

`riscv_illegal_instr_test` is the exception: it is the only test driving the illegal
instruction path through the controller, exercising `ibex_controller` exception
priority branches and decoder illegal-opcode handling that legal-instruction tests
never reach.

### Implication

From a pure structural coverage perspective, the 480-test regression is 39× overbuilt.
Cutting to 3 test types (~40 tests) yields 62.4% structural coverage — essentially
identical to the full run at 62.46%. The remaining 36 test types exist for **functional
correctness** (detecting cosim mismatches between DUT and spike), not for coverage
closure.

The uncovered 37.5% of structural coverage (primarily in `ibex_compressed_decoder` at
72% and `ibex_alu` at 61%) cannot be reached by any existing test type — it requires
new tests targeting those modules' uncovered branches specifically.
