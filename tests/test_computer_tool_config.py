import hashlib
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nexus_agent.computer_runtime import NexusComputerRuntime, RuntimeOperationError, _RuntimeInstanceLock


class ComputerToolConfigCASTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.runtime = NexusComputerRuntime(root=Path(self.folder.name) / "state")
        self.addCleanup(self.runtime.close)
        self.path = Path(self.folder.name) / "config.toml"
        self.resolver = patch.object(self.runtime, "_codex_path", return_value=self.path)
        self.resolver.start()
        self.addCleanup(self.resolver.stop)

    def write(self, content, before="", scope="tool.setup"):
        return self.runtime.dispatch("tool_setup.write_codex_config_cas", scope,
            {"content": content, "expected_revision": hashlib.sha256(before.encode()).hexdigest()})

    def test_real_write_replay_and_conflict_keep_original_data(self):
        content = 'model = "中文-model"\n'
        result = self.write(content)
        self.assertEqual(result["content"], self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.write(content, content)["revision"], result["revision"])
        with self.assertRaises(RuntimeOperationError) as raised:
            self.write('model = "stale"\n')
        self.assertEqual(raised.exception.code, "TOOL_CONFIG_CONFLICT")
        self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_wrong_scope_and_invalid_toml_do_not_write(self):
        for content, scope in [('model="x"', 'files.write'), ('broken = "sensitive', 'tool.setup')]:
            with self.assertRaises(RuntimeOperationError) as raised:
                self.write(content, scope=scope)
            self.assertNotIn("sensitive", str(raised.exception))
        self.assertFalse(self.path.exists())

    def test_concurrent_advisory_owner_rejects_second_writer(self):
        lock = _RuntimeInstanceLock(self.path.with_name(self.path.name + ".nexus-cas.lock"))
        lock.acquire()
        try:
            with self.assertRaises(RuntimeOperationError) as raised:
                self.write('model="blocked"')
            self.assertEqual(raised.exception.code, "TOOL_CONFIG_BUSY")
        finally:
            lock.release()
        self.assertFalse(self.path.exists())

    def test_io_failure_is_redacted_and_does_not_overwrite(self):
        self.write('model="old"')
        with patch("nexus_agent.computer_runtime._atomic_user_file_write", side_effect=OSError("private path and token")):
            with self.assertRaises(RuntimeOperationError) as raised:
                self.write('model="new"', 'model="old"')
        self.assertNotIn("private path", str(raised.exception))
        self.assertEqual(self.path.read_text(), 'model="old"')

    def test_crlf_config_revision_matches_normalized_read_endpoint(self):
        original = 'model="old"\r\n'
        self.path.write_bytes(original.encode())
        result = self.write('model="new"\r\n', original.replace('\r\n', '\n'))
        self.assertEqual(result['content'], 'model="new"\n')
        self.assertEqual(self.path.read_bytes(), b'model="new"\n')

    def test_oversized_old_file_is_rejected_without_replacement(self):
        from nexus_agent.computer_runtime import MAX_TEXT_RESULT
        original = b'#' + b'x' * MAX_TEXT_RESULT
        self.path.write_bytes(original)
        with self.assertRaises(RuntimeOperationError) as raised:
            self.write('model="new"')
        self.assertEqual(raised.exception.code, 'TOOL_CONFIG_TOO_LARGE')
        self.assertEqual(self.path.read_bytes(), original)

    def test_lock_path_permission_error_is_redacted(self):
        with patch.object(_RuntimeInstanceLock, 'acquire', side_effect=PermissionError('secret local path')):
            with self.assertRaises(RuntimeOperationError) as raised:
                self.write('model="new"')
        self.assertEqual(raised.exception.code, 'TOOL_CONFIG_WRITE_FAILED')
        self.assertNotIn('secret', str(raised.exception))

    def test_missing_revision_cannot_fall_back_to_legacy_write(self):
        with self.assertRaises(RuntimeOperationError) as raised:
            self.runtime.dispatch('tool_setup.write_codex_config_cas', 'tool.setup', {'content': 'model="x"'})
        self.assertEqual(raised.exception.code, 'TOOL_CONFIG_INVALID')
        self.assertFalse(self.path.exists())

    def test_readback_failure_is_not_reported_as_pre_write_conflict(self):
        def simultaneous_editor(path, content):
            path.write_bytes(content + b'\n# changed immediately after replacement\n')
        with patch('nexus_agent.computer_runtime._atomic_user_file_write', side_effect=simultaneous_editor):
            with self.assertRaises(RuntimeOperationError) as raised:
                self.write('model="written"')
        self.assertEqual(raised.exception.code, 'TOOL_CONFIG_VERIFICATION_FAILED')
        self.assertIn('model="written"', self.path.read_text())

    def fenced(self, sequence, *, barrier=False, content='model="fenced"', before='', runtime=None):
        runtime = runtime or self.runtime
        runtime.config['device_id'] = str(uuid.UUID(int=101))
        return runtime.dispatch('tool_setup.fence_codex_config' if barrier else 'tool_setup.write_codex_config_fenced',
            'tool.setup', {'namespace': runtime.config['device_id'], 'sequence': sequence,
                'content': content, 'expected_revision': hashlib.sha256(before.encode()).hexdigest()})

    def test_barrier_fences_delayed_write_across_runtime_restart(self):
        self.fenced(2)
        snapshot = self.fenced(5, barrier=True)
        self.assertEqual(snapshot['content'], 'model="fenced"')
        other = NexusComputerRuntime(root=Path(self.folder.name) / 'restarted')
        self.addCleanup(other.close)
        with patch.object(other, '_codex_path', return_value=self.path):
            for sequence in (2, 3, 5):
                with self.assertRaises(RuntimeOperationError) as raised:
                    self.fenced(sequence, runtime=other, content='model="delayed"', before='model="fenced"')
                self.assertEqual(raised.exception.code, 'TOOL_CONFIG_STALE_WRITE')
            self.fenced(6, runtime=other, content='model="new"', before='model="fenced"')
        self.assertEqual(self.path.read_text(), 'model="new"')

    def test_barrier_does_not_create_or_modify_configuration(self):
        result = self.fenced(1, barrier=True)
        self.assertFalse(self.path.exists())
        self.assertEqual(result['revision'], hashlib.sha256(b'').hexdigest())
        self.assertEqual(result['fence']['sequence'], 1)

    def test_corrupt_fence_fails_closed_and_does_not_disclose_contents(self):
        self.fenced(1)
        ledger = next(self.path.parent.glob('*.nexus-fence-*.json'))
        ledger.write_text('private-invalid-ledger', encoding='utf-8')
        with self.assertRaises(RuntimeOperationError) as raised:
            self.fenced(2, barrier=True)
        self.assertEqual(raised.exception.code, 'TOOL_CONFIG_FENCE_INVALID')
        self.assertNotIn('private-invalid', str(raised.exception))
        self.assertEqual(self.path.read_text(), 'model="fenced"')

    def test_fence_requires_bound_device_valid_sequence_and_scope(self):
        self.runtime.config['device_id'] = str(uuid.UUID(int=101))
        for namespace, sequence, scope in [(str(uuid.UUID(int=102)),1,'tool.setup'),
                (str(uuid.UUID(int=101)),True,'tool.setup'),
                (str(uuid.UUID(int=101)),0,'tool.setup'),
                (str(uuid.UUID(int=101)),1,'files.write')]:
            with self.assertRaises(RuntimeOperationError):
                self.runtime.dispatch('tool_setup.fence_codex_config',scope,{'namespace':namespace,'sequence':sequence})
        self.assertFalse(self.path.exists())

    def test_barrier_and_write_use_same_lock(self):
        lock = _RuntimeInstanceLock(self.path.with_name(self.path.name + '.nexus-cas.lock'))
        lock.acquire()
        try:
            with self.assertRaises(RuntimeOperationError) as raised:
                self.fenced(1, barrier=True)
            self.assertEqual(raised.exception.code, 'TOOL_CONFIG_BUSY')
        finally:
            lock.release()
        self.fenced(1, barrier=True)


if __name__ == "__main__":
    unittest.main()
