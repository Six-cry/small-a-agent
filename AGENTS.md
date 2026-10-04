# Project Instructions

Keep each subsystem's upgrade history beside that subsystem instead of placing all records in the repository root. When meaningful work begins on a subsystem that has no upgrade log yet, create a dedicated Markdown log in that subsystem's directory and keep updating it on later work.

For the `aa_my_agent` RAG system, read `aa_my_agent/rag/RAG_UPGRADE_LOG.md` before making changes. After every meaningful RAG experiment, implementation, rollback, or evaluation, append a record to that file.

Preserve historical findings and distinguish clearly between an isolated experiment and a change integrated into the production pipeline. Each record must include the problem, cause, change, verification method, actual result, current status, and remaining work.

A command completing successfully is not sufficient evidence that extracted PDF, table, formula, or image content is correct; compare representative output with the source document.
