# Social preview kit

`../social-preview.jpg` (1280×640, uploaded in the repo's Settings → Social preview) is rebuilt with:

```bash
python3 docs/social-preview/render.py --background docs/social-preview/background.jpg \
  --icon docs/social-preview/icon.svg --name "Personal|Finance" --out docs/social-preview.jpg
```

`render.py` needs Google Chrome (it lays the text out as HTML and screenshots it). `background.jpg` is an
AI-generated, text-free painting from this prompt:

> Abstract expressive oil painting filling the whole frame, wide 16:9: thick visible brushstrokes, canvas texture,
> soft painterly blending, diagonal sweeping strokes, editorial OpenAI-blog-cover aesthetic. ABSOLUTELY NO text,
> letters, numbers, logos, symbols, faces or objects. Keep the center calmer and mid-tone so white text stays
> readable. Palette: deep emerald and jade green with accents of coral, pink, turquoise and golden yellow.

`icon.svg`: three records merging into one event.
