"""Pinned 2.1 integration fix: stop injection at EOF, wait for callbacks.

Upstream LoadStoreTrace reports finished on enqueue, so tiny traces return
before any DRAM reads complete. All supported controllers retire reads/writes
through Request.callback; this preserves their native timing semantics.
"""
from pathlib import Path
p=Path('/opt/ramulator/src/ramulator/frontend/impl/memory_trace/loadstore_trace.cpp')
s=p.read_text()
pairs=[
 ('  size_t m_trace_count = 0;', '  size_t m_trace_count = 0;\n  size_t m_completed = 0;'),
 ('    init_trace(m_trace_path);', '    init_trace(m_trace_path);\n    m_stats.add("completed_requests", m_completed);'),
 ('  void tick() override {', '  void tick() override {\n    if (m_trace_count >= m_trace_length) return;'),
 ('    req.size_bytes = m_memory_system->get_tx_bytes();', '    req.size_bytes = m_memory_system->get_tx_bytes();\n    req.callback = [this](Request&) { ++m_completed; };'),
 ('return m_trace_count >= m_trace_length;', 'return m_completed >= m_trace_length;'),
]
for old,new in pairs:
    assert s.count(old)==1,old
    s=s.replace(old,new)
p.write_text(s)
