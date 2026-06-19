# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

RISCV-DV is a SV/UVM-based open-source random instruction generator for RISC-V processor verification. It generates randomized RISC-V assembly programs, compiles them, runs them on a Reference ISS (spike, ovpsim, whisper, sail-riscv), and compares the instruction traces to validate a DUT.

## Setup

```bash
pip3 install -r requirements.txt   # one-time dependency install
```

Or install as an editable package (makes `run` and `cov` available on PATH):

```bash
export PATH=$HOME/.local/bin/:$PATH
pip3 install --user -e .
```

Install Verible for linting:

```bash
verilog_style/build-verible.sh
```

## Common Commands

```bash
# Run the full flow (generate, compile, ISS sim, compare)
python3 run.py --target rv64gc --simulator vcs --iss spike

# Compile the generator only
python3 run.py --target rv64gc --simulator vcs --co

# Run a single named test
python3 run.py --target rv64gc --simulator vcs --iss spike --test riscv_rand_instr_test

# Run with pyflow (Python-only, no EDA tool required)
python3 run.py --target rv64gc --simulator pyflow --iss spike

# Run specific steps only: gen, gcc_compile, iss_sim, iss_cmp
python3 run.py --target rv64gc --simulator vcs --iss spike --steps gen,iss_sim

# Collect and merge coverage
python3 cov.py --target rv64gc --simulator vcs

# Run Verilog style check (Verible)
verilog_style/run.sh

# Build eUVM (D-language) port
cd euvm/build && make -j $(nproc)
```

## Architecture

### Three Parallel Implementations

The generator exists in three forms that mirror each other:

| Directory | Language | Simulator flag |
|-----------|----------|----------------|
| `src/` + `test/` | SystemVerilog / UVM | `--simulator vcs/questa/ius/...` |
| `pygen/pygen_src/` | Python (pyflow) | `--simulator pyflow` |
| `euvm/riscv/` | D / eUVM | euvm build system |

The SV implementation is the reference; pyflow and eUVM are ports. They share the same YAML configuration, the same `scripts/` Python utilities, and the same test flow orchestrated by `run.py`.

### Key Source Files (`src/`)

- `riscv_instr_pkg.sv` — Central package: all enums, typedefs, `include` of ISA instruction classes
- `riscv_instr_gen_config.sv` — `riscv_instr_gen_config`: the single randomized configuration object controlling instruction mix, privilege modes, memory layout, PMP, debug, etc.
- `riscv_asm_program_gen.sv` — Top-level program generator; orchestrates startup code, main/sub-programs, trap handlers, data sections
- `riscv_instr_sequence.sv` / `riscv_instr_stream.sv` — Building blocks for random and directed instruction streams
- `src/isa/` — One `.sv` file per instruction group (e.g. `rv32i_instr.sv`, `rv64m_instr.sv`, `riscv_vector_instr.sv`)
- `riscv_instr_cover_group.sv` — Functional coverage model

### Targets (`target/`)

Each subdirectory (e.g. `target/rv64gc/`, `target/rv32imc/`) contains:
- `riscv_core_setting.sv` — Defines `XLEN`, `SATP_MODE`, `supported_isa[]`, `supported_privileged_mode[]`, and unsupported instructions for a specific core configuration.
- `testlist.yaml` — Target-specific test list that extends or overrides the base list.

Pass `--custom_target <dir>` to point at a custom target directory; `--core_setting_dir` overrides just the SV settings file.

### Configuration YAMLs (`yaml/`)

- `simulator.yaml` — Compile and sim command templates for each supported tool (VCS, IUS, Questa, DSim, Riviera, pyflow). Placeholders like `<out>`, `<seed>`, `<cwd>` are substituted at runtime by `run.py`.
- `iss.yaml` — Command templates for each ISS (spike, ovpsim, whisper, sail).
- `base_testlist.yaml` — Canonical list of all standard tests with `gen_opts`, `iterations`, and `gen_test` fields.
- `cov_testlist.yaml` — Tests used specifically for coverage collection.

### Orchestration (`run.py`)

`run.py` drives the full pipeline:
1. **gen** — Invoke the SV/pyflow simulator to produce `.S` assembly files
2. **gcc_compile** — Cross-compile with `riscv-unknown-elf-gcc`
3. **iss_sim** — Run compiled ELF on the reference ISS
4. **iss_cmp** — Convert both logs to trace CSV format and compare

### Log Conversion (`scripts/`)

Each ISS has a dedicated log-to-CSV converter:
- `spike_log_to_trace_csv.py`
- `ovpsim_log_to_trace_csv.py`
- `whisper_log_trace_csv.py`
- `sail_log_to_trace_csv.py`
- `renode_log_to_trace_csv.py`

All produce the common format consumed by `instr_trace_compare.py`.

### Customization

- `user_extension/` — Drop-in hooks: `user_extension.svh` (add custom classes/overrides), `user_define.h` (preprocessor macros), `user_init.s` (boot-sequence assembly additions). Pass `--user_extension_dir` to point at an alternate directory.
- `src/isa/custom/` — Add custom instruction definitions here.

## Test Flow Notes

- `--steps` accepts a comma-separated subset of `gen,gcc_compile,iss_sim,iss_cmp` to run only part of the flow.
- `--seed` fixes the random seed; `--start_seed` sets a starting seed that increments per iteration; `--seed_yaml` reruns specific failing seeds.
- `--lsf_cmd` enables LSF job dispatch; without it, tests run locally in sequence.
- Output goes to `out/` by default; override with `-o`.
