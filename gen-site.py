#!/usr/bin/env python3
"""Regenerate a marketing site's llms-full.txt (all pages as markdown) and refresh its sitemap.xml lastmod.
Run after editing any page:  python gen-site.py compression   |   python gen-site.py quantecarlo   |   python gen-site.py all
llms.txt (the short index) is hand-written per site."""
import re, html, datetime, pathlib, sys

SITES = {
    "compression": {"base": "https://promptcompression.ai", "title": "promptcompression.ai",
                    "pages": [("index.html", "/", 1.0), ("how-it-works.html", "/how-it-works", 0.9), ("business-case.html", "/business-case", 0.9),
                              ("example.html", "/example", 0.9), ("findings.html", "/findings", 0.8), ("prompt-optimization.html", "/prompt-optimization", 0.6),
                              ("claude-code.html", "/claude-code", 0.8), ("contact.html", "/contact", 0.4)]},
    "quantecarlo": {"base": "https://quantecarlo.com", "title": "Quante Carlo",
                    "pages": [("index.html", "/", 1.0), ("how-it-works.html", "/how-it-works", 0.9), ("prompt-optimization.html", "/prompt-optimization", 0.9),
                              ("findings.html", "/findings", 0.8), ("about.html", "/about", 0.8), ("claude-code.html", "/claude-code", 0.6), ("contact.html", "/contact", 0.4)]},
}


def md(s, base):
    s = re.sub(r"<script.*?</script>|<style.*?</style>|<nav>.*?</nav>|<footer>.*?</footer>|<svg.*?</svg>", "", s, flags=re.S)
    s = re.sub(r"<img[^>]*alt=\"([^\"]*)\"[^>]*>", r"[figure: \1]", s)
    s = re.sub(r"<pre[^>]*><code>(.*?)</code></pre>|<pre[^>]*>(.*?)</pre>", lambda m: "\n```\n" + (m.group(1) or m.group(2)) + "\n```\n", s, flags=re.S)
    s = re.sub(r"<h1[^>]*>(.*?)</h1>", r"\n# \1\n", s, flags=re.S)
    s = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n## \1\n", s, flags=re.S)
    s = re.sub(r"<h3[^>]*>(.*?)</h3>", r"\n### \1\n", s, flags=re.S)
    s = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", s, flags=re.S)
    s = re.sub(r"<tr[^>]*>(.*?)</tr>", lambda m: "| " + " | ".join(re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", m.group(1), re.S)) + " |\n", s, flags=re.S)
    s = re.sub(r"<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", lambda m: f"[{m.group(2)}]({m.group(1) if m.group(1).startswith('http') else base + m.group(1)})", s, flags=re.S)
    s = re.sub(r"</p>|</div>|<br>", "\n", s); s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s); s = re.sub(r"[ \t]+", " ", s); s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def build(name):
    site, d, base, today = SITES[name], pathlib.Path(name), SITES[name]["base"], datetime.date.today()
    out = [f"# {site['title']} — full site\n", f"Generated {today}. Index: {base}/llms.txt\n"]
    urls = []
    for f, path, prio in site["pages"]:
        s = (d / f).read_text()
        title = re.search(r"<title>(.*?)</title>", s).group(1)
        out.append(f"\n\n---\n\n# {html.unescape(title)}\nURL: {base}{path}\n\n" + md(re.search(r"<body>(.*)</body>", s, re.S).group(1), base))
        urls.append(f"  <url><loc>{base}{path}</loc><lastmod>{today}</lastmod><priority>{prio}</priority></url>")
    (d / "llms-full.txt").write_text("\n".join(out) + "\n")
    (d / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + "\n".join(urls) + "\n</urlset>\n")
    print(name, "wrote llms-full.txt", len((d / "llms-full.txt").read_text()), "chars,", len(urls), "sitemap urls")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    for n in (SITES if which == "all" else [which]):
        build(n)
