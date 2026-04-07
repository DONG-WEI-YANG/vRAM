"""
End-to-end GPU inference with SD card offload
==============================================
RTX 5060 (8GB) + SD card (E:\) → run models that exceed VRAM
"""

import gc
import json
import os
import time
import zlib
import shutil
import torch

MB = 1024 ** 2
GB = 1024 ** 3


def gpu_status():
    if torch.cuda.is_available():
        free, total = torch.cuda.mem_get_info()
        return f"VRAM: {(total-free)/GB:.2f}/{total/GB:.1f}GB used"
    return "No CUDA"


def run_inference_test(
    model_id: str,
    offload_dir: str = None,
    load_in_4bit: bool = False,
    prompt: str = "The meaning of life is",
    max_new_tokens: int = 50,
    num_runs: int = 3,
    label: str = "",
):
    """Run inference and measure tokens/sec"""
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    print(f"\n{'='*60}")
    print(f"  {label or model_id}")
    print(f"  offload={offload_dir or 'none'}, 4bit={load_in_4bit}")
    print(f"  {gpu_status()}")
    print(f"{'='*60}")

    # Clear GPU
    gc.collect()
    torch.cuda.empty_cache()

    # Load model
    kwargs = {
        "device_map": "auto",
        "torch_dtype": torch.float16,
    }

    if load_in_4bit:
        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,
        )

    if offload_dir:
        os.makedirs(offload_dir, exist_ok=True)
        kwargs["offload_folder"] = offload_dir

    t_load_start = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(model_id, **kwargs)
    t_load_end = time.perf_counter()
    load_time = t_load_end - t_load_start

    print(f"  Model loaded in {load_time:.1f}s")
    print(f"  {gpu_status()}")

    # Check offload stats
    if offload_dir and os.path.isdir(offload_dir):
        offload_files = os.listdir(offload_dir)
        offload_size = sum(
            os.path.getsize(os.path.join(offload_dir, f))
            for f in offload_files
        )
        print(f"  Offloaded: {len(offload_files)} files, {offload_size/MB:.1f} MB")

    # Inference
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)

    # Warmup
    with torch.no_grad():
        model.generate(**inputs, max_new_tokens=5)

    # Timed runs
    token_counts = []
    times = []

    for run_i in range(num_runs):
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
            )
        torch.cuda.synchronize()
        t1 = time.perf_counter()

        new_tokens = outputs.shape[1] - inputs["input_ids"].shape[1]
        elapsed = t1 - t0
        token_counts.append(new_tokens)
        times.append(elapsed)

        if run_i == 0:
            text = tokenizer.decode(outputs[0], skip_special_tokens=True)
            print(f"  Output: {text[:100]}...")

    # Results
    avg_tokens = sum(token_counts) / len(token_counts)
    avg_time = sum(times) / len(times)
    ttft = times[0]  # first run includes any initial overhead
    tps = avg_tokens / avg_time

    result = {
        "model": model_id,
        "label": label,
        "load_in_4bit": load_in_4bit,
        "offload_dir": offload_dir or "none",
        "load_time_s": round(load_time, 1),
        "tokens_per_sec": round(tps, 2),
        "avg_time_s": round(avg_time, 3),
        "avg_tokens": int(avg_tokens),
        "ttft_s": round(ttft, 3),
        "num_runs": num_runs,
    }

    print(f"\n  Results:")
    print(f"    Load time:   {load_time:.1f}s")
    print(f"    tok/s:       {tps:.2f}")
    print(f"    TTFT:        {ttft:.3f}s")
    print(f"    {gpu_status()}")

    # Cleanup
    del model, tokenizer
    gc.collect()
    torch.cuda.empty_cache()

    return result


def main():
    sd_dir = "E:/.vram_offload"
    results = []

    print(f"GPU: {torch.cuda.get_device_name(0)}")
    print(f"VRAM: {torch.cuda.get_device_properties(0).total_memory/GB:.1f} GB")
    print(f"SD card offload dir: {sd_dir}")

    # Test 1: Small model, pure GPU (baseline)
    r = run_inference_test(
        "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
        offload_dir=None,
        load_in_4bit=False,
        label="TinyLlama 1.1B FP16 (pure GPU)",
    )
    results.append(r)

    # Test 2: Small model, 4-bit quantized on GPU
    r = run_inference_test(
        "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
        offload_dir=None,
        load_in_4bit=True,
        label="TinyLlama 1.1B INT4 (pure GPU)",
    )
    results.append(r)

    # Test 3: Small model, offload to SD card
    r = run_inference_test(
        "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
        offload_dir=sd_dir,
        load_in_4bit=False,
        label="TinyLlama 1.1B FP16 (SD offload)",
    )
    results.append(r)

    # Test 4: Larger model that needs offload - Phi-2 (2.7B, ~5.4GB FP16)
    r = run_inference_test(
        "microsoft/phi-2",
        offload_dir=None,
        load_in_4bit=False,
        label="Phi-2 2.7B FP16 (pure GPU)",
    )
    results.append(r)

    # Test 5: Phi-2 4-bit
    r = run_inference_test(
        "microsoft/phi-2",
        offload_dir=None,
        load_in_4bit=True,
        label="Phi-2 2.7B INT4 (pure GPU)",
    )
    results.append(r)

    # Test 6: Phi-2 FP16 with SD offload
    r = run_inference_test(
        "microsoft/phi-2",
        offload_dir=sd_dir,
        load_in_4bit=False,
        label="Phi-2 2.7B FP16 (SD offload)",
    )
    results.append(r)

    # Summary
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"  {'Config':<35} {'tok/s':>8} {'Load':>7} {'TTFT':>7}")
    print(f"  {'-'*60}")
    for r in results:
        print(f"  {r['label']:<35} {r['tokens_per_sec']:>8.2f} "
              f"{r['load_time_s']:>6.1f}s {r['ttft_s']:>6.3f}s")

    # Save
    out_path = "gpu_inference_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Results saved to {out_path}")

    # Cleanup SD offload
    if os.path.isdir(sd_dir):
        shutil.rmtree(sd_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
