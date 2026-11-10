"""Render a captured pyte terminal grid without changing its text or colors."""
from html import escape

PALETTE = dict(black='#000000', red='#cd0000', green='#00cd00', brown='#cdcd00',
               blue='#0000ee', magenta='#cd00cd', cyan='#00cdcd', white='#e5e5e5',
               brightblack='#7f7f7f', brightred='#ff0000', brightgreen='#00ff00',
               brightbrown='#ffff00', brightblue='#5c5cff', brightmagenta='#ff00ff',
               brightcyan='#00ffff', brightwhite='#ffffff')


def color(value, default):
    if value == 'default':
        return default
    return PALETTE.get(value, '#' + value)


def save_svg(screen, path):
    cell, line, margin = 10, 21, 16
    width, height = screen.columns * cell + margin * 2, screen.lines * line + margin * 2
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<title>Clean my Agent Rust terminal · isolated fictional fixtures</title>',
             f'<rect width="{width}" height="{height}" rx="12" fill="#081c1d"/>',
             '<g font-family="Menlo,Consolas,monospace" font-size="15" xml:space="preserve">']
    for y in range(screen.lines):
        row = screen.buffer[y]
        for x in range(screen.columns):
            char = row[x]
            foreground, background = color(char.fg, '#d5e9e3'), color(char.bg, '#081c1d')
            if char.reverse:
                foreground, background = background, foreground
            px, py = margin + x * cell, margin + y * line
            parts.append(f'<rect x="{px}" y="{py}" width="{cell}" height="{line}" fill="{background}"/>')
        for x in range(screen.columns):
            char = row[x]
            if not char.data.strip():
                continue
            foreground = color(char.bg, '#081c1d') if char.reverse else color(char.fg, '#d5e9e3')
            cells = 2 if x + 1 < screen.columns and row[x + 1].data == '' else 1
            weight = 'bold' if char.bold else 'normal'
            parts.append(f'<text x="{margin + x * cell}" y="{margin + y * line + 16}" fill="{foreground}" font-weight="{weight}" textLength="{cells * cell}" lengthAdjust="spacingAndGlyphs">{escape(char.data)}</text>')
    parts.append('</g></svg>')
    path.write_text('\n'.join(parts) + '\n', encoding='utf-8')
