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


Site-wide favicon consistency
-----------------------------
The SEO page builder now normalizes favicon links when it generates every
posts/<notion-page-id>/index.html page. It removes old icon, shortcut-icon,
Apple-touch-icon, mask-icon, and dated cache-busting declarations from the
post.html template, then emits these same stable root-relative links:

    /favicon.png

The website repository must keep the actual favicon.png file at its root and
use the same /favicon.png URL in index.html and blog.html. This automation
updates generated article pages; it does not rewrite the website homepage,
blog landing page, or the source post.html template. Existing Notion content,
Markdown files, workflow schedules, secrets, and the favicon image itself are
not changed by this favicon normalization.

Suggested automation-repository commit title
--------------------------------------------
Unify generated post favicon with site-wide branding

Suggested commit description
----------------------------
- Normalize favicon links in every generated Notion article page.
- Use the stable /favicon.png URL shared with the homepage.
- Remove stale cache-busted and duplicate icon declarations.
- Preserve article content, Notion data, schedules, and existing assets.

Validation of the favicon automation change
-------------------------------------------
- Python syntax compilation passed.
- Legacy rel="icon", rel="shortcut icon", rel="apple-touch-icon",
  rel="apple-touch-icon-precomposed", and rel="mask-icon" tags were tested.
- Non-icon links such as stylesheets and web manifests were preserved.
- Re-running favicon normalization did not duplicate tags.
- The website repository's post.html template from the supplied site archive
  passed the normalization test and produced only the two canonical links.

The full GitHub Actions workflow was not executed here because it requires
repository secrets and a live GitHub Actions run.
