#!/usr/bin/env python3
"""Regenerate llms-full.txt (all pages as markdown) and refresh sitemap lastmod. Run after editing any page."""
import re, html, datetime, pathlib
PAGES=[("index.html","/"),("how-it-works.html","/how-it-works"),("prompt-optimization.html","/prompt-optimization"),
       ("findings.html","/findings"),("claude-code.html","/claude-code"),("contact.html","/contact")]
BASE="https://promptcompression.ai"
def md(s):
    s=re.sub(r"<script.*?</script>|<style.*?</style>|<nav>.*?</nav>|<footer>.*?</footer>|<svg.*?</svg>","",s,flags=re.S)
    s=re.sub(r"<img[^>]*alt=\"([^\"]*)\"[^>]*>",r"[figure: \1]",s)
    s=re.sub(r"<pre[^>]*><code>(.*?)</code></pre>|<pre[^>]*>(.*?)</pre>",lambda m:"\n```\n"+(m.group(1) or m.group(2))+"\n```\n",s,flags=re.S)
    s=re.sub(r"<h1[^>]*>(.*?)</h1>",r"\n# \1\n",s,flags=re.S)
    s=re.sub(r"<h2[^>]*>(.*?)</h2>",r"\n## \1\n",s,flags=re.S)
    s=re.sub(r"<h3[^>]*>(.*?)</h3>",r"\n### \1\n",s,flags=re.S)
    s=re.sub(r"<li[^>]*>(.*?)</li>",r"- \1\n",s,flags=re.S)
    s=re.sub(r"<tr[^>]*>(.*?)</tr>",lambda m:"| "+" | ".join(re.sub(r"<[^>]+>","",c).strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>",m.group(1),re.S))+" |\n",s,flags=re.S)
    s=re.sub(r"<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",lambda m:f"[{m.group(2)}]({m.group(1) if m.group(1).startswith('http') else BASE+m.group(1)})",s,flags=re.S)
    s=re.sub(r"</p>|</div>|<br>","\n",s); s=re.sub(r"<[^>]+>","",s)
    s=html.unescape(s); s=re.sub(r"[ \t]+"," ",s); s=re.sub(r"\n\s*\n+","\n\n",s)
    return s.strip()
out=["# promptcompression.ai — full site\n",f"Generated {datetime.date.today()}. Index: {BASE}/llms.txt\n"]
for f,path in PAGES:
    s=pathlib.Path(f).read_text()
    title=re.search(r"<title>(.*?)</title>",s).group(1)
    out.append(f"\n\n---\n\n# {html.unescape(title)}\nURL: {BASE}{path}\n\n"+md(re.search(r"<body>(.*)</body>",s,re.S).group(1)))
pathlib.Path("llms-full.txt").write_text("\n".join(out)+"\n")
sm=pathlib.Path("sitemap.xml"); sm.write_text(re.sub(r"<lastmod>[^<]+</lastmod>",f"<lastmod>{datetime.date.today()}</lastmod>",sm.read_text()))
print("wrote llms-full.txt", len(pathlib.Path("llms-full.txt").read_text()), "chars")
