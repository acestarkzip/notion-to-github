SSS Robotics hashtag rendering fix
==================================

Replace the existing build_seo.py in the Notion-to-GitHub automation repository
with this patched build_seo.py. Do not replace the website repository's js/post.js
or post.html for this particular issue.

What this fixes
---------------
- A Markdown line such as:
    #OpenUSD #오픈USD #NVIDIA #PhysicalAI
  is treated as a hashtag paragraph, not an H1 heading.
- The first literal # is preserved in the generated HTML.
- Normal headings such as '# Heading' and '## Subheading' remain unchanged.
- Fenced code blocks are not modified.

After replacing the file in the automation repository, run the existing ALL
workflow once. The build step recreates the static posts from the Markdown files,
so existing pages and future pages receive the same correction.

This patch changes only build_seo.py. It does not edit Markdown source files,
Notion content, workflow schedules, secrets, or the separate website source code.

Validation performed
--------------------
- Python syntax compilation passed.
- Hashtag-row transformation checks passed for the EP.02 format, ordinary
  headings, short hashtag strings, and fenced code examples.
