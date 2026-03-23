# Expanding GPU VRAM with NVMe: ProX PC & MiPhi AI Breakthrough

**URL:** https://www.proxpc.com/blogs/expanding-gpu-vram-with-nvme-prox-pc-miphi-ai-breakthrough

---

Products
Solutions
Services
Resources
Company
0
011-40727769
Home
Blogs
Expanding GPU VRAM with NVMe: ProX PC & MiPhi AI Breakthrough
Expanding GPU VRAM with NVMe: ProX PC & MiPhi AI Breakthrough
By:
Divyansh Rawat
March 18, 2026
SHARE

We all have, at least once, had this thought while working on AI: what if we could upgrade GPU VRAM just like we upgrade our system storage? What if we could expand the GPU memory pool using NVMe?

Well, that thought is a reality today. We have catapulted entirely past the theory phase and completed exhaustive testing in the ProX PC labs, and only after verifying the hard data are we claiming this success and sharing this breakthrough with you.

The GPU VRAM Bottleneck

Whenever we attempt to run massive parameter models locally, the memory wall halts our progress. For a long time, the standard approach to holding massive weights in memory involved stacking multiple enterprise GPUs together.

However, relying entirely on traditional GPU scaling creates a very specific architectural flaw alongside severe physical and financial barriers:

Forced Compute Scaling: As model sizes increase without proportional growth in GPU VRAM, memory becomes the limiting factor. This forces organizations to scale by adding more GPUs and nodes bringing additional networking and compute overhead.

Massive Financial Investment: Acquiring multiple enterprise-grade GPUs demands an enormous hardware budget, especially when you only needed the memory component.

Infrastructural Strain: Multi-GPU clusters produce massive thermal output and require highly complex cooling solutions.

Extreme Power Consumption: Stacking computational hardware exponentially increases the electrical draw of the server rack.

Our Solution in partnership with MiPhi

At ProX PC, our engineering team dedicated months to analyzing this specific bottleneck. We actively sought a more intelligent architectural approach that expands memory pools logically. This pursuit led us to a strategic partnership with MiPhi, who sent us their specialized 2x 1TB aiDAPTIVCache NVMe drives directly to our testing labs.

Why MiPhi aiDAPTIVCache drives are different from standard NVMe: Standard SSDs are designed for occasional storage and would fail within days under the constant data-swapping required by AI. MiPhi’s specialized architecture is purpose-built for this task:

Extreme Endurance (100 DWPD): Traditional enterprise SSDs are rated for 1 to 3 Drive Writes Per Day (DWPD). MiPhi’s aiDAPTIVCache uses specialized SLC NAND to achieve a massive 100 DWPD rating. This allows it to handle hundreds of terabytes of data movement daily without wearing out.

How It Operates: Training & Inference

The aiDAPTIV+ architecture adapts dynamically depending on the specific phase of your AI workflow. It manages data differently to optimize performance for both model development and final deployment.

The Training Phase: Slicing the model during fine-tuning, massive parameter models demand immense memory capacity. The aiDAPTIVLink middleware acts as the essential bridge between the PyTorch library and your system's hardware to handle this load.

Callback Interception: PyTorch contains internal callbacks that trigger when the system reaches its physical VRAM limit. The aiDAPTIVLink middleware actively intercepts these signals to maintain continuous operation.

Strategic Placement: aiDAPTIVLink Middleware divides the large model into smaller segments, known as slices. The system places the active slices directly onto the physical GPU for immediate mathematical processing. Simultaneously, all the remaining pending slices wait securely on the aiDAPTIVCache (NVMe).

Continuous Swapping: As soon as the GPU finishes a processing round on an active slice, the middleware moves that data back to the aiDAPTIVCache. It immediately pulls the next pending slice onto the GPU to continue the compute cycle.

The Inference Phase: During inference, the system shifts from model execution to efficient reuse of Key-Value (KV) cache, treating it as a persistent memory layer rather than a temporary GPU artifact.

1. Traditional Limitation (GPU-bound KV Cache)

In standard deployments, when a request is processed:

KV cache is created in GPU VRAM.

Due to limited memory, this cache is evicted when space is needed.

If the same prompt or context appears again, the model must:

Recompute the entire KV cache from scratch.

This increases Time To First Token (TTFT) and wastes compute.

2. aiDAPTIV+ Approach (Persistent KV Cache)

Instead of discarding KV cache, we store it in aiDAPTIVCache.

This transforms KV cache into a reusable, persistent artifact:

When a similar or identical request comes in:

The system retrieves KV cache directly from aiDAPTIVCache.

Avoids recomputation entirely.

GPU memory is now used only for active decoding, not long-term storage.

Key Advantages
a. Reduced Re-computation

Eliminates repeated KV generation for identical or overlapping prompts.

b. Improved TTFT (Time To First Token)

Since prefill is skipped or reduced:

Responses start significantly faster.

c. Scalable Context Handling

NVMe provides orders of magnitude more capacity than VRAM.

Enables caching of:

Long documents

Multi-turn conversations

Reusable prompt templates

d. Efficient Multi-user Serving

Shared KV cache across users:

Popular prompts or documents don’t need recomputation per user.

3.RAG (Retrieval-Augmented Generation) Use Cases

This is where the approach becomes especially powerful.

Traditional RAG Bottleneck

Every query:

Retrieve documents

Recompute KV cache for the retrieved context

This becomes expensive when:

Same documents are repeatedly retrieved

Multiple users query similar knowledge bases

4.With aiDAPTIV+

Instead of transferring KV cache using the inference engine’s small native page size, aiDAPTIV+ operates at a configurable chunk level, typically much larger. This allows the system to better utilize the available bandwidth between aiDAPTIVCache and GPU memory, resulting in more efficient data movement and faster KV cache loading.

a.Persistent Document-Level KV Cache

Precompute KV cache for:

Knowledge base documents.

PDFs, manuals, embeddings context.

Store them in aiDAPTIVCache once.

b.Query-Time Optimization

At inference:

Instead of recomputing document KV cache:

Fetch cached KV directly from aiDAPTIVCache.

Only compute KV for the new query tokens

5. Multi-User RAG Scenario

In enterprise systems:

Multiple users query the same document (e.g., internal docs, policies)

With aiDAPTIV+ KV caching:

Shared document KV cache is reused across all users

Avoids:

Redundant computation

GPU memory pressure

6. Practical Benefits in RAG Systems

Faster responses for repeated queries

Higher throughput for concurrent users

Enables large-scale, low-latency RAG deployments

Now, we move from the architecture to the exact data. Our engineering team at ProX PC put this integration through rigorous testing to see exactly how it performs under heavy, enterprise-level workloads.

Physical Limits of Localized AI Fine-Tuning

To understand the magnitude of these results, we must look at the hardware requirements of fine-tuning a large parameter platform like Llama 3.1 70B. Fine-tuning this 70B platform in FP32 precision requires roughly 1.4TB of total memory to securely hold the base weights alongside the massive additional overhead of gradients and optimizer states.
A standard RTX PRO 6000 provides 96GB of VRAM, and an RTX 5090 provides 32GB. Attempting to run this 1.4TB fine-tuning workload on a single GPU immediately hits an out-of-memory error. To run this setup using a traditional architecture, an organization is forced to purchase and link a massive multi-GPU cluster.
By utilizing the 2x 1TB MiPhi aiDAPTIVCache NVMe drives as an active VRAM pool, we bypassed this physical limit entirely on a single-node ProX PC server. The NVMe pool successfully held the parameter weights and fine-tuning states, paging them to the active GPU memory with high efficiency.

Benchmarks: Llama 3.1 Platform Fine-Tuning Tests
Precision	Platform	GPU	Tokens/s	Power Draw	NVMe Utilization (2x 1TB)
FP16	Llama-3.1-8B	RTX PRO 6000	~2,973 – 3,386 +1	408W	417GB
FP16	Llama-3.1-8B	RTX 5090	~2,337 +1	563W	407GB
FP16	Llama-3.1-70B	RTX PRO 6000	400	560W	1.6TB
FP16	Llama-3.1-70B	RTX 5090	~190 +1	508W	1.4TB
FP32	Llama-3.1-8B	RTX PRO 6000	797.266	475W	560GB
FP32	Llama-3.1-8B	RTX 5090	607.136	510W	407GB
FP32	Llama-3.1-70B	RTX PRO 6000	770.425	390W	1.6TB
FP32	Llama-3.1-70B	RTX 5090	40.997	514W	1.4TB

Note on scaling: We also pushed the system to load the Llama-3.1-405B for fine-tuning evaluation. This massive model exceeded our initial 2TB NVMe storage limit and system was unable to run it.

Summary

Our RTX PRO 6000 benchmarks prove that resolving the GPU memory bottleneck through aiDAPTIV+ is highly effective. A single GPU can now handle massive workloads like Llama 3.1 70B fine-tuning that previously required an entire server rack.

By creating the massive memory pool through aiDAPTIV+ with intelligent PyTorch integration, we empower industries to achieve true localized AI fine-tuning.

At ProX PC, we are fully integrating MiPhi aiDAPTIV+ technology into our hardware ecosystem and We sincerely thank the team at MiPhi for this partnership. By collaborating closely, we built a practical, powerful solution that fundamentally expands the possibilities of localized AI deployment.

Here are answers to some frequently asked questions-

How to increase GPU VRAM without upgrading the GPU?

Integrate aiDAPTIV+ middleware with specialized NVMe drives to create a unified memory pool. This allows a single GPU to process massive models like Llama 3.1 70B by using storage as logical VRAM.

Can NVMe be used as VRAM?

Yes, aiDAPTIVLink middleware connects your software to the hardware to swap data "slices" between the GPU and NVMe. The system intercepts memory signals to maintain operation even when the physical VRAM limit is reached.

What is GPU memory expansion?

This technology uses high-endurance SLC NAND drives as an active memory layer for the GPU. It bypasses physical hardware limits by storing model weights and training states on NVMe instead of requiring multiple expensive GPUs.

Is NVMe faster than VRAM?

VRAM remains the fastest location for immediate mathematical processing. However, aiDAPTIV+ makes NVMe highly efficient by paging only the necessary data to the GPU for each compute cycle, providing the massive capacity that standard VRAM lacks.

Can a standard SSD replace GPU memory?

Standard enterprise SSDs fail quickly because they lack the endurance for constant AI data swapping. You must use specialized drives with a 100 DWPD (Drive Writes Per Day) rating to handle these heavy workloads safely.

Contact us for this solution
📞 011-40727769
✉️ sales@proxpc.com
or check out our Server solutions
WRITTEN BY
Divyansh Rawat

Divyansh Rawat is the Content Manager at ProX PC, where he combines a filmmaker’s eye with a lifelong passion for technology. Gravitated towards tech from a young age, he now drives the brand's storytelling and is the creative force behind the video content you see across our social media channels.

Share this:

Related Posts
View more
How GPU Servers Can Benefit Your Business?

Harness the power of GPU servers to revolutionize business operations with unparalleled performance in AI, data analysis, and rendering tasks.

Read More
NVIDIA GeForce RTX 4090 Vs RTX 3090 Deep Learning Benchmark

The power of NVIDIA® GeForce RTX 4090: Redefining deep learning benchmarks, exceeding expectations set by the RTX 3090

Read More
FOR PROFESSIONALS, BY PROFESSIONALS

Discover ProX PC for best custom-built PCs, powerful workstations, and GPU servers in India. Perfect for creators, professionals, and businesses. Shop now!

COMPANY
About Us
Blogs
Contact Us
Careers
PRODUCTS
Workstations
GPU Server
Edge Computing
SOLUTIONS
View All Solutions
INFO LINKS
Terms & Conditions
Shipping Policy
Return & Refund Policy
Product Warranty And Support
SERVICES
View All Services
Managed Services
Business Services
Home Services
Medium & Large Business Services
CONTACT US
011-40727769
sales@proxpc.com

1st Floor, H9, Block B1, Mathura Rd, Block B, Mohan Cooperative Industrial Estate, Badarpur, New Delhi,
Delhi 110044

WE ACCEPT
Terms Of UsePrivacy Policy
© 2020 - 2025 ProX PC. ALL RIGHTS RESERVED.
Chat with us