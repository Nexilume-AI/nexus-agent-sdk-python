# KernelFoundry MCP

Compile, test, benchmark and evolve CUDA kernels remotely using native
KernelFoundry on a Pascal SM60 GPU. A coding agent can repeatedly edit and test
the kernel, or request autonomous optimization with a caller-approved model.

## Workflow

1. Create a private workspace and load the ReLU demo or upload your two files.
2. `task.py` defines a KernelFoundry `TestBase` class, reference, build method,
   correctness tests and performance tests. `kernel.cu` contains the CUDA
   implementation with `[EVOLVE_START]` and `[EVOLVE_END]` markers.
3. Validate the package, then call `build_and_test` for real compilation and
   reference/kernel evaluation. No model authorization is needed for this step.
4. For autonomous optimization, call `authorize_kernelfoundry_provider` and
   explicitly approve your own Nexus Router or OpenAI-compatible model. Secure
   web authorization and explicit MCP preauthorization are supported. Never
   paste a model key into tool arguments.
5. Call `submit_task` with the authorization ID, goal, limits and a fresh request
   UUID. Reuse that UUID only to retry the same submission.
6. Follow `job_status`, or open the private background console for optimization
   events. The console requires the model credential owner's personal login.
7. Download kernel source, reports, evaluation history, logs and SQLite results.
   Close the workspace when finished.

## Runtime and limits

- Pinned upstream: `0b2338891b7b4f34e0d3a1ab930c7543046a06c9`.
- CUDA 12.4, PyTorch 2.5.1, NumPy; Pascal SM60. This deployment exposes CUDA;
  Intel SYCL/OpenCL, Triton and custom package installation are not enabled.
- The service supplies configuration and pytest integration. Upload `task.py`
  and `kernel.cu`, each at most 64 KiB; arbitrary YAML and host paths are rejected.
- Caller code and generated kernels run in separate containers with no network,
  no provider keys or broker socket, nonroot execution, a read-only root,
  12 GiB host RAM and 4 CPUs. One GPU evaluation at a time; admission waits if
  the selected GPU is busy. Other system workloads may affect timings.
- One queued/active job per verified caller, FIFO across callers; two private
  workspaces per caller, eight overall, 24-hour idle expiry. The same API key
  identifies the same caller. Workspace capabilities are also required.
- Autonomous runs use one branch and 1–20 iterations, up to 55 minutes per job,
  up to iterations + 2 model requests, requesting 4,096 output tokens per call.
  Provider charges apply; these limits are not a monetary budget.
- Pascal profiling is disabled. Results contain actual correctness outcomes
  and GPU timing, not hardware-counter or roofline certification.
- Download links require single-use redemption within ten minutes and issue
  a one-hour download session with Range, ETag and SHA256. Links are revocable;
  renew through MCP to resume after expiry.

Correctness and speedup depend on your tests, reference and benchmark shapes.
A successful demo validates the integration, not universal kernel correctness
or guaranteed acceleration. Review generated code before integrating it.

## Verified deployment

The hosted integration was tested through the public MCP endpoint with a real
gpt-6-sol authorization, one autonomous iteration, a newly generated ReLU CUDA
kernel, three correctness cases and two GPU benchmark shapes. Compilation and
correctness passed. This short acceptance run did not improve performance over
the PyTorch reference; it is an integration check, not a quality or speed claim.

Independent hosted integration of Apache-2.0 KernelFoundry. Free Marketplace
access; model inference is billed by your chosen provider.
