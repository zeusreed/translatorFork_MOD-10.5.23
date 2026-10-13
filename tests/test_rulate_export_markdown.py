from gemini_translator.ui.dialogs.rulate_export import EPUBConverterThread


def test_html_to_plain_text_drops_style_blocks():
    converter = EPUBConverterThread("book.epub")
    html = """
    <html>
      <head>
        <style>
          p.p2 {margin: 0.0px 0.0px 12.0px 0.0px; font: 12.0px Times; -webkit-text-stroke: #000000}
          span.s1 {font-kerning: none}
        </style>
      </head>
      <body>
        <p class="p2"><span class="s1">Нормальный текст главы.</span></p>
      </body>
    </html>
    """

    text = converter._html_to_plain_text(html)

    assert text == "Нормальный текст главы."
    assert "p.p2" not in text
    assert "span.s1" not in text
