"""Declare directly used CUDA libraries before baseline validation and commit."""
import json
from pathlib import Path


def repair_cuda_manifest(workspace):
    workspace = Path(workspace)
    kernel = workspace / 'kernel.py'
    manifest = workspace / 'solution.json'
    if not kernel.is_file() or not manifest.is_file():
        return False
    source = kernel.read_text()
    value = json.loads(manifest.read_text())
    spec = value.get('spec', {})
    dependencies = spec.get('dependencies')
    roles = value.get('dependency_roles')
    if not isinstance(dependencies, list) or not isinstance(roles, dict):
        return False  # Let the existing validator report malformed manifests.
    required = {}
    if 'nvrtcCompileProgram' in source:
        required['nvrtc'] = 'Compiles the embedded self-authored CUDA source to PTX.'
    if 'cuLaunchKernel' in source:
        required['cuda-driver'] = 'Loads PTX and launches the self-authored CUDA kernel.'
    changed = False
    for name, role in required.items():
        if name not in dependencies:
            dependencies.append(name)
            changed = True
        if not roles.get(name):
            roles[name] = role
            changed = True
    if changed:
        manifest.write_text(json.dumps(value, indent=2) + '\n')
    return changed


def install():
    path = Path('/opt/atrex/orchestrator/campaign.py')
    source = path.read_text()
    anchor = '        candidate_blob = git_worktree_blob(self.workspace, "kernel.py")'
    start = source.index('    def _framework_baseline_problem(')
    at = source.index(anchor, start)
    patch = ('        from nexus_manifest import repair_cuda_manifest\n'
             '        repair_cuda_manifest(self.workspace)\n')
    assert patch not in source, 'Manifest compatibility hook already installed'
    source = source[:at] + patch + source[at:]
    compile(source, str(path), 'exec')
    path.write_text(source)


if __name__ == '__main__':
    install()
