"""Emails that come with only HTML: the plain text LyteKeepr makes keeps each link (Vicky's "video" link was dropped)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gmail.parser import html_to_text, normalize_message  # noqa: E402


class HtmlToTextTest(unittest.TestCase):
    def test_link_kept_next_to_its_words(self):
        html = ('<html><head><style>p{color:red}</style></head><body><div>Peter,</div><div><br></div>'
                '<div>Here’s the&nbsp;<a href="https://youtu.be/Qg_PdNg06fU">video</a>&nbsp;of my daughter’s homework.</div>'
                '<!--[if mso]><img src="https://t.example/open"><![endif]--><div>On Oct 28, Peter &lt;peter@nexyai.app&gt; wrote:</div></body></html>')
        text = html_to_text(html)
        self.assertIn("Here’s the video (https://youtu.be/Qg_PdNg06fU) of my daughter’s homework.", text)
        self.assertIn("Peter <peter@nexyai.app> wrote:", text)
        self.assertTrue(text.startswith("Peter,\n"))
        self.assertNotIn("color:red", text)
        self.assertNotIn("&nbsp;", text)

    def test_link_shown_once_when_words_are_the_address(self):
        self.assertEqual(html_to_text('<a href="https://nexyai.app">https://nexyai.app</a>'), "https://nexyai.app")
        self.assertEqual(html_to_text('<a href="mailto:a@b.c">write me</a>'), "write me")

    def test_gmail_html_only_email_gets_text(self):
        import base64
        data = base64.urlsafe_b64encode(b'<p>See <a href="https://x.y/z">this</a></p>').decode()
        m = normalize_message({"id": "1", "payload": {"mimeType": "text/html", "headers": [], "body": {"data": data}}}, "a@b.c")
        self.assertEqual(m["textBody"], "See this (https://x.y/z)")


if __name__ == "__main__":
    unittest.main()
