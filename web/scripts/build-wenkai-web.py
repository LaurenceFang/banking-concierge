"""Build web-only WenKai WOFF2 Unicode shards; preserve OFL notices and source TTF.
Run from any directory with Python fonttools[woff] installed.
"""
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools import subset
root = Path(__file__).resolve().parents[1]
source = root / 'public/fonts/LXGWWenKai-Regular.ttf'
output = root / 'public/fonts/wenkai-web'
output.mkdir(exist_ok=True)
font = TTFont(source)
points = sorted(font.getBestCmap())
groups = {}
for cp in points:
    groups.setdefault(cp // 512, []).append(cp)
rules = []
for group, chars in groups.items():
    font = TTFont(source)
    options = subset.Options()
    options.flavor = 'woff2'
    options.name_IDs = ['*']
    options.name_legacy = True
    options.name_languages = ['*']
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes=chars)
    subsetter.subset(font)
    font.flavor = 'woff2'
    filename = f'wenkai-{group:03x}.woff2'
    font.save(output / filename)
    ranges = f'U+{group * 512:X}-{group * 512 + 511:X}'
    rules.append(f"@font-face{{font-family:WenKai;src:url('/fonts/wenkai-web/{filename}') format('woff2');font-style:normal;font-weight:400;font-display:swap;unicode-range:{ranges}}}")
(root / 'src/wenkai-fonts.css').write_text('\n'.join(rules)+'\n',encoding='utf-8')
print(f'{len(groups)} web-only shards; {sum(p.stat().st_size for p in output.glob("*.woff2")):,} bytes total')
