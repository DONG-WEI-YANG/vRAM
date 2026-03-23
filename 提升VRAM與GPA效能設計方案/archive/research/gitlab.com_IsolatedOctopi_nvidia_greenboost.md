
No that long ago I took neural network and machine learning lessons, trained a couple of recognition models, and got far enough into the TensorFlow Developer certification path that I was essentially ready to sit the exam. I got into a relationship, live changed, less control of the time, and I ended up missing the certification exam. That one still stings a bit.

I use AI inference every day at work. Watching tokens burn through cloud APIs, knowing I had a GPU doing nothing useful, it eventually clicked. The question I started asking was simple: can I run a model like glm-4.7-flash properly on my own machine, with the GPU tensor cores doing all the compute, without CPU spill?

That was the spark for GreenBoost to materialize.

The first thing I needed to understand was whether NVIDIA's tensor cores could handle the full model if I found a way to give the GPU access to more memory.

That led me to NVIDIA's open-source kernel modules source, the code published on GitHub.

The NVIDIA kernel modules handle the low-level hardware side memory management, unified virtual memory, DMA. CUDA sits on top of that as the runtime and API layer: it's what applications actually talk to when they want to allocate memory, move data, or run compute on the GPU. Tensor cores computations, transfers, allocations, all of it flows through CUDA API layer.

And NVIDIA publishes all of that source code openly. Everything I needed was right there.

That code was invaluable for getting the DMA-BUF import path right.

Then on my first tries something was not working as expected, Ollama was still doing whatever it wanted, ignoring GreenBoost entirely, as if it wasn't there.

Exploring a bit further is when I found I should intercept Ollama call because that is what I'm using nowadays, though I am aware the AI inference landscape is much broader.

I explored documentation to get as much information on ollama project as I could reach, I was looking for how llama.cpp handles CPU offload, how ExLlamaV2/V3 structures its KV cache, and how vLLM's paged attention allocates memory.

Gathering this information was decisive to know how those allocation lifecycles work and where in greenboost stack should be the code to intercept the call, I gathered different opensource libraries to get a broad scenario so that any inference tool would work without modification, not just Ollama, that had been the goal.

and that's it

Synapse Terminal (a non opensource local AI Inference terminal), started to answer my questions in a proper pace, I was stonished at the beggining, could not believe glm-4.7-flash:q8_0 was working that relatively well (later improved).

Another thing I had in mind is that the OS should be optimized as much as possible. That's why I created the tune-all command to optimize the system as much as possible for local AI inference. This command runs by default when choosing the full-install command.

I do have a modified (non official BIOS) that includes latest intel microcode, since the brand of my motherboard hasn't released any newer version, for you to have an idea on how much I like optimization.

There is still much more to do, I guess. I don't know when my hardware will surprise me again by pushing past what I thought were its limits.

I personally have much to learn about AI Inference. That's why this project has a tools section, to dive further into AI inference.

Hope you all enjoy greenboost kernel modules.

Installation
git clone https://gitlab.com/IsolatedOctopi/nvidia_greenboost.git
cd nvidia_greenboost

# Auto-detects GPU VRAM, RAM size, CPU P/E-core topology, NVMe capacity
# and computes optimal parameters at install time:
sudo ./greenboost_setup.sh full-install

# You can skip the GitLab version check with:
sudo ./greenboost_setup.sh full-install --skip-update-check

# After reboot, verify everything is working:
sudo ./greenboost_setup.sh diagnose

The installer then runs the following steps automatically:

Step	What it does
Version check	Queries GitLab tags API; if a newer release exists, offers to git pull and restart — skipped with --skip-update-check or when offline
Purge	Stops Ollama/llama-server, unloads any previous GreenBoost module, removes old install artifacts
Dependencies	Installs build tools, kernel headers, CUDA dev packages
Build + install	Compiles greenboost.ko + libgreenboost_cuda.so, installs to /lib/modules and /usr/local/lib
Load	Inserts the kernel module with auto-detected pool sizes (T1/T2/T3)
System configs	Writes Ollama systemd env vars, NVMe udev rules, CPU governor service, hugepages, sysctl
Tune-all	CPU governor → performance; NVMe scheduler → none, read-ahead → 4 MB; THP → always; vm.swappiness=10; GRUB params (rcu_nocbs, nohz_full, transparent_hugepage=always); AI/compute libraries (OpenBLAS AVX2, hwloc, libnuma, nvtop, microcode)
Tools	Creates /opt/greenboost/venv and installs Python inference tools: ExLlamaV3 (alternative inference engine with GreenBoost-aware KV cache), kvpress, LoRA fine-tuning utilities, and model optimization helpers
Restart	Restarts any services that were stopped during purge (Ollama, llama-server)

greenboost_setup.sh detects Red Hat-based systems at runtime and delegates to greenboost_setup_rocky.sh (contributed by Alan Sill, see contributors below).

Quick usage
# Run Ollama as normal — GreenBoost is transparent
ollama run glm-4.7-flash:q8_0

# Monitor memory tiers live:
watch -n1 'cat /sys/class/greenboost/greenboost/pool_info'

For tools that require manual model loading, see implementing_further_into_tools.md.

Ollama does not need this; GreenBoost integrates with it automatically via the CUDA shim.

What GreenBoost is not
It is not a replacement for the NVIDIA driver. nvidia.ko, nvidia-uvm.ko, and all NVIDIA official modules continue to run exactly as normal. GreenBoost loads beside them.
It is not a virtual GPU. It does not expose a new GPU device or change how compute works. It only affects how CUDA memory allocations are routed.
It is not a hack around driver restrictions. The DMA-BUF + external memory import path it uses is a documented CUDA feature.
It does not work without the NVIDIA driver installed.
Usage reference
sudo ./greenboost_setup.sh [--skip-update-check] <command>

GLOBAL FLAGS:
  --skip-update-check  Skip GitLab version check (offline / air-gapped workstations)

COMMANDS:
  install              Build and install module + CUDA shim system-wide
  uninstall            Unload, remove module + all config files
  build                Build only (no system install)
  load                 Load module with default 3-tier parameters
  unload               Unload module (keeps installed files)
  tune                 Tune system for LLM workloads (governor, NVMe, THP, sysctl)
  tune-grub            Fix GRUB boot params (THP=always, rcu_nocbs, nohz_full…)
  tune-sysctl          Consolidate sysctl files + apply compute-optimized knobs
  tune-libs            Install missing AI/compute libraries (OpenBLAS, hwloc…)
  tune-all             Run tune + tune-grub + tune-sysctl + tune-libs in sequence
  install-sys-configs  Install Ollama env, NVMe udev, CPU governor, hugepages, sysctl
  install-deps         Install all Ubuntu OS packages (build + CUDA + AI libs)
  setup-swap [GB]      Create/activate NVMe swap (default: auto-sized)
  full-install         Complete install — deps, module, shim, configs, tune-all, Python inference tools
  status               Show module status and 3-tier pool info
  diagnose             Full health check — run this after reboot to verify everything works
  optimize-model [--model M] [--strategy tensorrt|lora|exllama|all]
                       Optimize LLM for max speed: TRT-LLM, LoRA, ExLlamaV3
  help                 Show this help

CUDA SHIM WRAPPER:
  greenboost run <app> [args...]   Run a CUDA app with GreenBoost overflow enabled
Using GreenBoost inside containers, VMs, and WSL2

On bare metal, the greenboost kernel module handles everything. For environments where greenboost.ko cannot be loaded; Docker, LXC, KVM guests, WSL2, shared HPC clusters, the CUDA shim automatically falls back to Path B: cuMemHostRegister(DEVICEMAP), which pins anonymous system RAM pages directly through the CUDA driver with no kernel module required. Check Jerry Nguyen's contribution (see Contributors), for full explanation of path selection, bandwidth trade-offs, and per-environment instructions see container_vm_mode.md.

Model optimization tools

The tools/ directory bundles several open-source libraries for further tuning; quantization, KV cache compression, fine-tuning, and alternative inference engines. See tools/README.md for details.

License

GPL v2 — open-source. Attribution to Ferran Duarri is required in all forks, derivatives, and any documentation that references this work.

Copyright (C) 2026 Ferran Duarri
Contributors

Alan Sill (@alansill) ; contributed greenboost_setup_rocky.sh, a setup script for Red Hat-based systems (Rocky Linux, AlmaLinux, RHEL).

Jerry Nguyen (@phubao) ; contributed the kernel-module-free overflow path, the cuMemHostRegister(DEVICEMAP) approach that enables GreenBoost VRAM extension inside containers, VMs, WSL2, and HPC clusters without requiring greenboost.ko. Integrated as Path B of the blended shim in v2.5.