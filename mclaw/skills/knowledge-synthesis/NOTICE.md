# Knowledge Synthesis attribution

- Author: Anthropic.
- Upstream: https://github.com/anthropics/knowledge-work-plugins/tree/f652327d4e953d7957d99e735c3c7f9964e9d2e5/enterprise-search/skills/knowledge-synthesis
- Revision: `f652327d4e953d7957d99e735c3c7f9964e9d2e5`.
- License: Apache-2.0. The upstream `enterprise-search/LICENSE` is included verbatim
  as `LICENSE`; its copyright appendix contains an unfilled template, so no
  specific copyright year or notice is inferred here.

Modified by M-Claw on 2026-10-15: translated and reorganized the upstream
instructions into `SKILL.md` and `references/synthesis-guide.md`; replaced
enterprise connector placeholders with M-Claw's available file, attachment,
search and extraction tools; removed the plugin-specific `user-invocable: false`
field so the skill follows M-Claw's normal discovery and explicit-use behavior.
Preserved deduplication, thematic synthesis, attribution, conflict reporting and
size-aware summarization. Refined freshness and authority handling to distinguish
effective dates, drafts, source independence and incomplete extraction. Added
workspace output guidance and M-Claw package metadata. Modified instruction files
carry a change notice.
