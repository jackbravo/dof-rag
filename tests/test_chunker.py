"""Regression tests for rag_poc.chunker preamble preservation.

Before the fix, _split_by_heading silently dropped the preamble (content
before the first heading). For H2_COMPOUND documents whose only H2s are the
trailing rubric signatures, that discarded the entire document body and the
document ended up with zero chunks (breaking the daily updater's verify()).
"""

import unittest

from rag_poc.chunker import split_text

# Plain paragraphs only: no H2/H3/bold lines, no table rows, so padding does
# not change how classify() sees the document.
PARAGRAPH = (
    "En observancia a la Constitución Política de los Estados Unidos "
    "Mexicanos en su artículo 134, y de conformidad con la Ley de "
    "Adquisiciones, Arrendamientos y Servicios del Sector Público, se "
    "convoca a los interesados en participar en la licitación pública.\n\n"
)


def _pad(text: str, min_bytes: int = 7000) -> str:
    while len(text.encode("utf-8")) < min_bytes:
        text += PARAGRAPH
    return text


def _split(text: str):
    return split_text(text, len(text.encode("utf-8")), "test-doc")


class H2CompoundPreambleTest(unittest.TestCase):
    def test_trailing_rubric_only_h2_keeps_body(self):
        """Docs whose only H2s are trailing rubrics must not lose the body."""
        body = _pad(
            "SECRETARIA DE ENERGIA\n\n### CONVOCATORIA 006\n\n" + PARAGRAPH
        )
        text = body + "\n\n##  RUBRICA.\n\n\n## (R.- 123456)\n"
        chunks = _split(text)
        self.assertTrue(chunks, "expected chunks, got none")
        joined = "\n".join(c.text for c in chunks)
        self.assertIn("SECRETARIA DE ENERGIA", joined)
        self.assertIn("artículo 134", joined)

    def test_preamble_has_no_invented_heading_prefix(self):
        body = _pad("# PODER EJECUTIVO\n\n# SECRETARIA DE GOBERNACION\n\n")
        text = body + "## PRIMER DECRETO\n\n" + _pad(PARAGRAPH) + "\n## SEGUNDO DECRETO\n\nContenido.\n"
        chunks = _split(text)
        self.assertTrue(chunks)
        self.assertTrue(chunks[0].text.startswith("# PODER EJECUTIVO"))
        for chunk in chunks:
            self.assertFalse(chunk.text.startswith("## \n"), repr(chunk.text[:40]))
            self.assertNotIn("### \n\n", chunk.text)

    def test_h3_preamble_inside_h2_section_is_kept(self):
        section_body = _pad("Texto introductorio de la sección.\n\n" + PARAGRAPH)
        text = (
            "## PRIMER DECRETO\n\n"
            + section_body
            + "\n### Artículo primero\n\n"
            + _pad(PARAGRAPH)
            + "\n## SEGUNDO DECRETO\n\nContenido.\n"
        )
        chunks = _split(text)
        self.assertTrue(chunks)
        joined = "\n".join(c.text for c in chunks)
        self.assertIn("Texto introductorio de la sección", joined)

    def test_regular_compound_doc_still_splits_by_h2(self):
        text = (
            "## PRIMER DECRETO\n\n"
            + _pad(PARAGRAPH)
            + "\n## SEGUNDO DECRETO\n\n"
            + _pad("Contenido del segundo decreto.\n\n" + PARAGRAPH)
        )
        chunks = _split(text)
        self.assertTrue(chunks)
        self.assertTrue(any(c.text.startswith("## PRIMER DECRETO") for c in chunks))
        self.assertTrue(any(c.text.startswith("## SEGUNDO DECRETO") for c in chunks))


if __name__ == "__main__":
    unittest.main()
