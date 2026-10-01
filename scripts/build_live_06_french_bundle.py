from __future__ import annotations

import csv
import hashlib
import json
import textwrap
import zipfile
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
import re


APP_DIR = Path(__file__).resolve().parent.parent
SIM_CASE_DIR = APP_DIR / "simulated-case"
DATA_DIR = SIM_CASE_DIR / "1_actual_demo_data"
LIVE_EMAILS_DIR = DATA_DIR / "Live_Test_Emails"
LIVE_ATTACHMENTS_DIR = DATA_DIR / "Live_Attachments"
MANIFEST_DIR = SIM_CASE_DIR / "2_preparatory_control" / "Manifests"
PACKAGING_DIR = SIM_CASE_DIR / "3_packaging_reference"
DEMO_PREP_DIR = APP_DIR / "demo-prep" / "simulated case"

LIVE_EMAIL_FILENAME = "LIVE_06_2026-04-28_french_relocation_declaration_packet.eml"
LIVE_EMAIL_DATE = "Tue, 28 Apr 2026 18:22:00 -0500"
LIVE_EMAIL_SUBJECT = "French declaration draft and 52-page exhibit packet for Spain relocation request"
P13_FILENAME = "P13_French_Relocation_Declaration_and_Temporary_School_Transfer_Request.pdf"
P14_FILENAME = "P14_English_Message_Exhibit_on_Proposed_Montreal_Transfer.pdf"


@dataclass
class PdfPage:
    title: str
    paragraphs: list[str]
    style: str = "document"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wrap_paragraphs(paragraphs: list[str], width: int = 92) -> list[str]:
    lines: list[str] = []
    for paragraph in paragraphs:
        if not paragraph:
            lines.append("")
            continue
        wrapped = textwrap.wrap(paragraph, width=width, break_long_words=False, break_on_hyphens=False) or [""]
        lines.extend(wrapped)
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _pdf_escape(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return escaped.encode("cp1252", errors="replace")


def _append_rect(parts: list[bytes], x: float, y: float, w: float, h: float, color: tuple[float, float, float]) -> None:
    parts.append(f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg\n".encode("ascii"))
    parts.append(f"{x:.1f} {y:.1f} {w:.1f} {h:.1f} re f\n".encode("ascii"))


def _append_text_block(
    parts: list[bytes],
    *,
    font: str,
    size: int,
    leading: int,
    x: float,
    y: float,
    lines: list[str],
    color: tuple[float, float, float] = (0, 0, 0),
) -> None:
    parts.append(b"BT\n")
    parts.append(f"/{font} {size} Tf\n".encode("ascii"))
    parts.append(f"{leading} TL\n".encode("ascii"))
    parts.append(f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg\n".encode("ascii"))
    parts.append(f"{x:.1f} {y:.1f} Td\n".encode("ascii"))
    for idx, line in enumerate(lines):
        parts.append(b"(" + _pdf_escape(line) + b") Tj\n")
        if idx != len(lines) - 1:
            parts.append(b"T*\n")
    parts.append(b"\nET\n")


def _page_number_from_title(title: str) -> int | None:
    match = re.search(r"Page\s+(\d+)", title)
    return int(match.group(1)) if match else None


def _content_stream_for_chat_page(page: PdfPage) -> bytes:
    phone_x = 108
    phone_y = 70
    phone_w = 396
    phone_h = 652
    header_h = 54
    footer_h = 22
    inner_x = phone_x + 16
    top_y = phone_y + phone_h
    page_no = _page_number_from_title(page.title)

    parts: list[bytes] = []
    _append_rect(parts, 0, 0, 612, 792, (0.96, 0.96, 0.96))
    _append_rect(parts, phone_x, phone_y, phone_w, phone_h, (0.90, 0.93, 0.89))
    _append_rect(parts, phone_x, top_y - header_h, phone_w, header_h, (0.11, 0.52, 0.38))
    _append_rect(parts, phone_x, phone_y, phone_w, footer_h, (0.94, 0.94, 0.94))

    _append_text_block(parts, font="F2", size=12, leading=14, x=inner_x, y=top_y - 24, lines=["WhatsApp"], color=(1, 1, 1))
    _append_text_block(
        parts,
        font="F1",
        size=9,
        leading=11,
        x=inner_x + 86,
        y=top_y - 22,
        lines=["Eric Brooks"],
        color=(1, 1, 1),
    )
    footer_label = f"P14 exhibit page {page_no}" if page_no else "P14 exhibit"
    _append_text_block(parts, font="F1", size=8, leading=10, x=phone_x + 145, y=phone_y + 8, lines=[footer_label], color=(0.32, 0.32, 0.32))

    meta_lines = textwrap.wrap(page.title.replace("P14 Page ", "Exhibit page "), width=34, break_long_words=False)
    chip_w = 220
    chip_h = 16 + len(meta_lines) * 10
    chip_x = phone_x + (phone_w - chip_w) / 2
    current_y = top_y - header_h - 18
    _append_rect(parts, chip_x, current_y - chip_h + 6, chip_w, chip_h, (0.86, 0.86, 0.86))
    _append_text_block(parts, font="F1", size=8, leading=10, x=chip_x + 10, y=current_y - 10, lines=meta_lines, color=(0.28, 0.28, 0.28))
    current_y -= chip_h + 12

    body_paragraphs = page.paragraphs
    for idx, paragraph in enumerate(body_paragraphs):
        stripped = paragraph.strip()
        if stripped.startswith("System:"):
            text = stripped[len("System:"):].strip()
            wrapped = textwrap.wrap(text, width=40, break_long_words=False, break_on_hyphens=False) or [""]
            bubble_width = 240
            bubble_height = 14 + len(wrapped) * 12
            bubble_x = phone_x + (phone_w - bubble_width) / 2
            bubble_y = current_y - bubble_height
            _append_rect(parts, bubble_x, bubble_y, bubble_width, bubble_height, (0.85, 0.85, 0.85))
            _append_text_block(parts, font="F1", size=8, leading=10, x=bubble_x + 12, y=bubble_y + bubble_height - 12, lines=wrapped, color=(0.32, 0.32, 0.32))
            current_y = bubble_y - 10
            if current_y < phone_y + 70:
                break
            continue

        is_right = stripped.startswith("Eric")
        bubble_width = 214 if len(paragraph) < 180 else 244
        wrapped = textwrap.wrap(paragraph, width=34 if bubble_width == 214 else 40, break_long_words=False, break_on_hyphens=False) or [""]
        bubble_height = 18 + len(wrapped) * 12
        bubble_x = phone_x + phone_w - bubble_width - 18 if is_right else phone_x + 18
        bubble_y = current_y - bubble_height
        bubble_color = (0.86, 0.97, 0.81) if is_right else (1.0, 1.0, 1.0)
        _append_rect(parts, bubble_x, bubble_y, bubble_width, bubble_height, bubble_color)
        _append_text_block(parts, font="F1", size=9, leading=11, x=bubble_x + 10, y=bubble_y + bubble_height - 14, lines=wrapped, color=(0.10, 0.10, 0.10))

        time_label = "8:14 PM" if page_no == 27 and idx == 1 else f"7:{(page_no or 10 + idx) % 60:02d} PM"
        _append_text_block(
            parts,
            font="F1",
            size=6,
            leading=8,
            x=bubble_x + bubble_width - 28,
            y=bubble_y + 5,
            lines=[time_label],
            color=(0.40, 0.40, 0.40),
        )
        current_y = bubble_y - 10
        if current_y < phone_y + 70:
            break

    return b"".join(parts)


def _content_stream_for_page(page: PdfPage) -> bytes:
    if page.style == "chat":
        return _content_stream_for_chat_page(page)
    lines = wrap_paragraphs(page.paragraphs)
    title_lines = textwrap.wrap(page.title, width=58, break_long_words=False, break_on_hyphens=False) or [page.title]
    parts: list[bytes] = []
    parts.append(b"BT\n")
    parts.append(b"/F2 14 Tf\n")
    parts.append(b"72 744 Td\n")
    parts.append(b"17 TL\n")
    for title_line in title_lines:
        parts.append(b"(" + _pdf_escape(title_line) + b") Tj\n")
        parts.append(b"T*\n")
    parts.append(b"/F1 11 Tf\n")
    parts.append(b"14 TL\n")
    for line in lines:
        if line:
            parts.append(b"(" + _pdf_escape(line) + b") Tj\n")
        parts.append(b"T*\n")
    parts.append(b"ET\n")
    return b"".join(parts)


def build_pdf_bytes(title: str, pages: list[PdfPage]) -> bytes:
    objects: list[bytes] = []

    def add_object(payload: bytes) -> int:
        objects.append(payload)
        return len(objects)

    font_regular = add_object(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    font_bold = add_object(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )

    page_refs: list[int] = []
    content_refs: list[int] = []
    pages_ref_num = len(objects) + (2 * len(pages)) + 1

    for page in pages:
        stream = _content_stream_for_page(page)
        content_ref = add_object(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        content_refs.append(content_ref)
        page_ref = add_object(
            (
                f"<< /Type /Page /Parent {pages_ref_num} 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >> "
                f"/Contents {content_ref} 0 R >>"
            ).encode("ascii")
        )
        page_refs.append(page_ref)

    pages_payload = f"<< /Type /Pages /Count {len(page_refs)} /Kids [{' '.join(f'{ref} 0 R' for ref in page_refs)}] >>".encode(
        "ascii"
    )
    pages_ref = add_object(pages_payload)
    if pages_ref != pages_ref_num:
        raise RuntimeError("Unexpected PDF object ordering while building pages tree.")

    catalog_ref = add_object(f"<< /Type /Catalog /Pages {pages_ref} 0 R >>".encode("ascii"))
    info_ref = add_object(
        (
            "<< /Author (Synthetic DLYTE Demo) /Creator (build_live_06_french_bundle.py) "
            f"/Title ({title}) /Producer (Minimal PDF writer) >>"
        ).encode("cp1252", errors="replace")
    )

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{idx} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_ref} 0 R /Info {info_ref} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf)


def french_filing_pages() -> list[PdfPage]:
    return [
        PdfPage(
            "Declaration supplementaire en francais concernant la demande de retour en Espagne avec l'enfant",
            [
                "Circuit Court of Lake County, Illinois. Brooks v. Brooks. Version de travail preparee pour revue cliente avant service.",
                "Je, Natalie Claire Brooks, declare sous serment que je sollicite une autorisation de retour en Espagne avec l'enfant des parties, Ava Rose Brooks, avant la rentree scolaire de septembre 2026.",
                "La presente declaration est fournie en francais afin de verifier la coherence d'un ensemble documentaire bilingue comportant une declaration narrative en francais et un paquet d'exhibits en anglais.",
            ],
        ),
        PdfPage(
            "Page 2 - Resume de la demande",
            [
                "La demande vise le retour de la Mere et de l'Enfant en Espagne avant la rentree scolaire de septembre 2026, avec un logement deja reserve et un depart planifie pour la fin juillet.",
                "Je ne demande aucune modification immediate d'une ordonnance deja en vigueur tant qu'aucune decision n'est rendue. Je demande seulement que la Cour considere la demande comme une proposition structuree, appuyee par des messages, des confirmations de voyage, des echanges proceduraux et des elements relatifs a l'employabilite en Espagne.",
                "Les pieces de soutien pertinentes figurent dans P14, notamment les conversations des pages 1 et 38 a 44 concernant le consentement et les conditions discutees, les pages 13 a 15 concernant le depart reserve, les pages 21 a 23 concernant l'employabilite et les pages 24 a 28 concernant la position ecrite du pere.",
            ],
        ),
        PdfPage(
            "Page 3 - Motifs professionnels et scolaires",
            [
                "Le retour en Espagne permettrait a la Mere de reintegrer rapidement un marche du travail ou son profil a deja ete evalue comme competitif pour des roles de direction operationnelle et strategique.",
                "L'Enfant approche d'une rentree charniere. La demande soutient qu'une installation avant septembre 2026 reduirait le risque de rupture pedagogique en cours de cycle et offrirait un cadre linguistique stable en francais.",
                "La demande n'affirme pas qu'un retour est deja approuve. Elle affirme qu'il existe un projet concret, documente et susceptible d'etre encadre par des modalites parentales detaillees.",
            ],
        ),
        PdfPage(
            "Page 4 - Logement, reseau de soutien et soins",
            [
                "Un logement de transition en Espagne a deja ete reserve pour la Mere et l'Enfant a compter du 31 juillet 2026. Les confirmations de reservation et de transport figurent dans P14.",
                "Le projet comprend la proximite d'une ecole, un acces aux soins pediatriques et un reseau de soutien maternel local pouvant contribuer a la stabilite quotidienne de l'Enfant.",
                "Les echanges de P14 montrent aussi que la Mere a evoque la continuite des appels video, des informations scolaires et des besoins medicaux comme conditions de mise en oeuvre pratique.",
            ],
        ),
        PdfPage(
            "Page 5 - Echanges entre les parties",
            [
                "Je reconnais qu'Eric n'a jamais signe d'entente finale. En revanche, les pages 1 et 38 a 44 de P14 montrent plusieurs messages dans lesquels il dit etre pret a envisager un retour en Espagne et demande plutot des conditions concretes.",
                "Le passage le plus important apparait a la page 1 de P14, ou Eric ecrit qu'il est pret a laisser la Mere et l'Enfant vivre en Espagne et qu'il reflechira aux conditions rendant cela possible.",
                "Ces messages ne valent pas consentement judiciaire. Ils sont pertinents parce qu'ils contredisent l'idee selon laquelle le projet aurait toujours ete refuse sans discussion.",
            ],
        ),
        PdfPage(
            "Page 6 - Proposition parentale pendant le sejour temporaire",
            [
                "Je propose que la Cour considere des appels video reguliers, une communication scolaire periodique et des rencontres physiques organisees dans la ville de residence de l'Enfant, selon des modalites precises a fixer par ordonnance.",
                "Les pages 16 a 18, 29 a 30 et 38 a 44 de P14 montrent deja des discussions sur les appels video, les disponibilites et les questions pratiques que le pere souhaitait voir clarifiees.",
                "Je demande que la Cour traite ces options comme des propositions parentales detaillees plutot que comme une tentative d'effacement du role d'Eric.",
            ],
        ),
        PdfPage(
            "Page 7 - Conclusion",
            [
                "Pour ces motifs, je demande que la Cour autorise le retour en Espagne de la Mere et de l'Enfant avant la rentree scolaire de septembre 2026, sous reserve d'un cadre parental defini et d'une continuite informationnelle et medicale documentee.",
                "Je demande subsidiairement que la Cour reconnaisse que P14 constitue un paquet d'exhibits utile pour interpreter les discussions reelles entre les parties, sans traiter ces messages comme une ordonnance ou un accord definitif.",
                "Je confirme que la presente declaration de travail ne modifie pas a elle seule les obligations financieres, les dates d'audience ou l'etat du dossier.",
            ],
        ),
        PdfPage(
            "Page 8 - References aux exhibits",
            [
                "P14 page 1 et pages 38 a 44: conversations sur le retour en Espagne et les conditions evoquees par Eric.",
                "P14 pages 2 a 3 et 13 a 15: confirmations de logement et de voyage pour la fin juillet 2026.",
                "P14 pages 4 a 6 et 51 a 52: proces-verbaux et ordonnances procedurales du Tribunal.",
                "P14 pages 21 a 23: confirmations relatives a l'employabilite de la Mere en Espagne.",
                "P14 pages 24 a 28: declaration assermentee du pere et bordereau de ses exhibits.",
                "P14 pages 29 a 37: appels, photos et mouvements financiers invoques. P14 pages 45 a 50: texte personnel verse par le pere au soutien de sa position.",
            ],
        ),
    ]


def french_email_body() -> str:
    return (
        "Natalie,\n\n"
        "Attached are the two documents we discussed for the Spain relocation request packet.\n\n"
        "1. A French draft declaration that states the request in narrative form.\n"
        "2. A 52-page English exhibit packet that ties the request back to concrete exchanges, travel confirmations, procedural records and related supporting documents.\n\n"
        "Please read the declaration for factual accuracy first. Then use the exhibit packet only to confirm that the page references and quoted messages line up.\n\n"
        "Nothing in this packet changes the existing parenting arrangement or hearing dates by itself. This is still a party request, not an order.\n\n"
        "The key pages for review are page 1 and pages 38-44 in the exhibit packet, especially the written statement that he was ready to let you live in Spain with Ava subject to conditions.\n\n"
        "Dana"
    )


def english_review_text() -> str:
    return (
        "# English review companion for P13\n\n"
        "This file is for review only. The demo-facing filing remains the French PDF.\n\n"
        "1. Natalie asks for permission to return to Spain with Ava before the September 2026 school year.\n"
        "2. The request is tied to a concrete travel plan, a reserved temporary residence and evidence that Spain-based work options are viable.\n"
        "3. The current case status remains unchanged unless a court changes it.\n"
        "4. The English exhibit packet P14 is the factual support set.\n"
        "5. Page 1 and pages 38-44 of P14 are the key conversation pages about prior written openness to the Spain move.\n"
        "6. Pages 13-15 cover the outbound flight booking; pages 21-23 cover employability support; pages 24-28 cover Eric's sworn response.\n"
        "7. The filing asks the court to treat the relocation proposal as a structured, evidenced request, not as a final move already approved.\n"
    )


def exhibit_pages() -> list[PdfPage]:
    return [
        PdfPage(
            "P14 Page 1 - Spain relocation consent exchange",
            [
                "Natalie, Wed Jan 14 10:50 AM: Looking ahead, how do you picture our co-parenting working if I have to return to my home country this summer?",
                "Eric, 5:33 PM: In terms of summer travel I assume you would want to take Ava with you. In terms of co-parenting it depends on how you see your future here.",
                "Natalie, 6:04 PM: I mean leave the country for good this summer and live in Spain.",
                "Eric, 8:20 PM: I can see that Ava is very attached to you and that she loves you very much. I believe it is better for her if she stays with you. For that reason I am ready to let you go to live in Spain. Let me think about the conditions that would make that possible.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 2 - Airbnb receipt shared in chat",
            [
                "Natalie: I found a place for us for August, sending you the confirmation.",
                "System: Image attached - Airbnb reservation receipt",
                "Natalie: One month, July 31 to August 30, one adult and one child, Girona.",
                "Eric: Got it. Can you send the address too?",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 3 - Airbnb property details shared in chat",
            [
                "Natalie: Here are the details on the place.",
                "System: Image attached - listing details",
                "Natalie: Two bedrooms, host lives nearby, quiet street. No pets, checkout by 11.",
                "Natalie: It's just a stopgap until we find something longer term.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 4 - Hearing minutes discussed in chat, June 5 2026 (part 1)",
            [
                "Eric: Did you get the minutes from the June 5 appearance?",
                "Natalie: Yes, the judge noted the relocation issue as contested.",
                "Natalie: We're on the record for it now, not just talking about it privately.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 5 - Hearing minutes discussed in chat, June 5 2026 (part 2)",
            [
                "Natalie: The order says I'm seeking to move with Ava to Spain and that it needs to be heard on the merits.",
                "Eric: What about the interim financial stuff?",
                "Natalie: That's in there too, plus a new schedule for filings.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 6 - Hearing minutes discussed in chat, June 5 2026 (part 3)",
            [
                "Natalie: There are deadlines for both of us now on the filings.",
                "Natalie: The interim parenting schedule stays the same until the relocation question is decided.",
                "Eric: Understood, I'll follow the dates on my end too.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 7 - Old trip photo shared in chat",
            [
                "Eric: Found this from the trip a couple years back.",
                "System: Image attached - photo, Eric and Ava at the airport",
                "Eric: She was so excited about the window seat.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 8 - Old trip itinerary shared in chat",
            [
                "Eric: Here's the old itinerary from that one, for the record.",
                "System: Image attached - boarding pass, outbound segment",
                "Eric: Seattle to St. Louis, direct flight down.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 9 - Old trip return itinerary shared in chat",
            [
                "Eric: And the way back.",
                "System: Image attached - boarding pass, return segment",
                "Eric: Same booking, just the return leg.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 10 - Old trip fare and payment shared in chat",
            [
                "Eric: For what it's worth, here's what that trip cost.",
                "System: Image attached - fare and payment summary",
                "Eric: Paid out of my own account, ticket numbers are on there.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 11 - Old check-in notice shared in chat",
            [
                "Eric: Also digging up the check-in email from that other trip.",
                "System: Image attached - airline check-in notice",
                "Eric: Different trip, same year.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 12 - Old check-in confirmation shared in chat",
            [
                "Eric: Route and confirmation code are both on there.",
                "System: Image attached - airline confirmation details",
                "Natalie: Why does this matter now?",
                "Eric: Just keeping a full record, that's all.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 13 - Spain departure booking shared in chat",
            [
                "Natalie: Booked our flight for July 30, overnight to Barcelona.",
                "System: Image attached - flight booking confirmation",
                "Natalie: Just me and Ava on this one.",
                "Eric: That's a lot sooner than I thought.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 14 - Spain departure booking fare details shared in chat",
            [
                "Natalie: Here's the fare breakdown if you want it.",
                "System: Image attached - fare class and baggage details",
                "Natalie: Economy, one checked bag each.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 15 - Spain departure booking payment shared in chat",
            [
                "Natalie: And the payment confirmation.",
                "System: Image attached - payment confirmation",
                "Natalie: It's paid in full, this one is happening.",
                "Eric: Noted.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 16 - Court email discussed in chat (overview)",
            [
                "Eric: I emailed the court about how we're handling responses before the July 24 hearing.",
                "Natalie: What did you propose?",
                "Eric: Just the timing and format, nothing about the merits yet.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 17 - Court email discussed in chat (filing mechanics)",
            [
                "Eric: I asked for written submissions a week before the hearing date.",
                "Eric: And a shared deadline so neither of us is filing at the last minute.",
                "Natalie: Fine by me.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 18 - Court email discussed in chat (practical limits)",
            [
                "Eric: I told the court that if you leave for Spain my options for regular contact get very limited.",
                "Eric: I proposed weekly video calls and a set holiday schedule as a minimum.",
                "Natalie: That's not a final agreement.",
                "Eric: I said the same thing in the email.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 19 - Pediatric receipt shared in chat",
            [
                "Natalie: Took Ava to urgent care, sending the receipt.",
                "System: Image attached - pediatric urgent care receipt",
                "Natalie: She's fine, just wanted you to know.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 20 - Pediatric receipt charges shared in chat",
            [
                "Natalie: Here's the charge breakdown if you want to split it.",
                "System: Image attached - itemized charges",
                "Eric: Send me your e-transfer, I'll cover my half.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 21 - Job outreach shared in chat",
            [
                "Natalie: A recruiter reached out about an operations role based in Spain.",
                "System: Image attached - recruiter message",
                "Natalie: Starting late this year if it goes anywhere.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 22 - Job outreach follow-up shared in chat",
            [
                "Natalie: They followed up with some notes on my profile.",
                "System: Image attached - recruiter follow-up",
                "Natalie: Said my background fits the Spain market roles well.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 23 - Employment counselling note shared in chat",
            [
                "Natalie: Talked to an employment counsellor too, forwarding her notes.",
                "System: Image attached - counsellor feedback",
                "Natalie: She thinks my experience realistically fits data or strategy roles out there.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 24 - Eric's response discussed in chat (opening)",
            [
                "Eric: Filing my response to the safeguard application today.",
                "Eric: I'm describing myself as an active parent and contesting the urgency claim.",
                "Natalie: Noted.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 25 - Eric's response discussed in chat (claims)",
            [
                "Eric: I'm also laying out my financial support history in there.",
                "Eric: And saying the urgency around this was created by the timeline, not an emergency.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 26 - Eric's response discussed in chat (conclusions)",
            [
                "Eric: Bottom line, I'm asking the court to deny the relocation for now.",
                "Eric: Keep current access as is, and put off a final decision on the merits.",
                "Natalie: I saw the filing.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 27 - Exhibit list shared in chat",
            [
                "Eric: For the record, here's what I'm attaching to my response.",
                "Eric: Call screenshots, some photos, the payment records, our Spain conversation, and something I wrote for Ava.",
                "Natalie: Okay.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 28 - Filing cover note shared in chat",
            [
                "Eric: Cover sheet is filed, everything's attached.",
                "System: Document attached - filing cover sheet",
                "Eric: Let me know if you don't get a copy from the court.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 29 - Call screenshot thread A",
            [
                "Natalie, 11:27 PM: Ava is asking where you are. I need to call you.",
                "Eric, 11:29 PM: I am safe and well and ready for a call with her if she is available within the next twenty minutes.",
                "Natalie, 11:36 PM: We just got back. She asked if she was going to see you today.",
                "Eric, 11:55 PM: I am ready in twenty. I am going to bed soon.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 30 - Call screenshot thread B",
            [
                "Natalie: Was that missed call for Ava?",
                "Eric: Yes.",
                "System: Video call, 19 minutes",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 31 - Photos shared in chat, set 1",
            [
                "Eric: A few from this week.",
                "System: Image attached - Ava on the walk to the metro",
                "System: Image attached - Ava waiting at the platform",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 32 - Photos shared in chat, set 2",
            [
                "Eric: Playground after school today.",
                "System: Image attached - Ava on the slide",
                "System: Image attached - Ava and Eric at the swings",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 33 - Photos shared in chat, set 3",
            [
                "Eric: She wanted me to send you this one.",
                "System: Image attached - Ava holding a drawing",
                "Natalie: Cute, tell her I said so.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 34 - Photos shared in chat, set 4",
            [
                "Eric: Bedtime story night.",
                "System: Image attached - Ava reading in bed",
                "Eric: She picked the book herself.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 35 - Photos shared in chat, set 5",
            [
                "Eric: A couple more from the weekend.",
                "System: Image attached - Ava at the park",
                "System: Image attached - Ava with ice cream",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 36 - Payment app screenshot shared in chat A",
            [
                "Eric: Sending the transfer history so it's all in one place.",
                "System: Image attached - payment app activity",
                "Eric: Food, apartment, daycare, it's all labeled in the memo lines.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 37 - Payment app screenshot shared in chat B",
            [
                "Eric: A few more from earlier this year, larger transfers in there too.",
                "System: Image attached - payment app activity, continued",
                "Natalie: I have my own records too.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 38 - Spain conversation screenshot A",
            [
                "Eric: There are a few practical things I would like to understand so we are aligned as you think about the move.",
                "Eric: Whenever you are ready, could you share the city, the housing plan, schooling, healthcare, support network and rough timeline?",
                "Natalie: My priority right now is other matters related to Ava and our future.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 39 - Spain conversation screenshot B",
            [
                "Eric: I have a few important topics to discuss in relation to Ava.",
                "Natalie: I missed your call. If this is about your previous request, I am not able to deal with that paperwork right now.",
                "Natalie: My priority at the moment is other matters related to Ava and our future.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 40 - Spain conversation screenshot C",
            [
                "Eric: I am not looking for final answers yet, just your general plan on housing, schooling, pediatric care and timing.",
                "Eric: The more concrete it is, the easier it will be to assess what is possible.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 41 - Spain conversation screenshot D",
            [
                "Eric: I can see that Ava is very attached to you and that she loves you very much.",
                "Eric: I believe it is better for her if she stays with you. For that reason I am ready to let you go live in Spain.",
                "Eric: Let me think on the conditions for that to be possible. I will get back to you.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 42 - Spain conversation screenshot E",
            [
                "Natalie: It is going to require some time to plan our departure.",
                "Natalie: I would expect a clearer picture closer to April and I will keep you informed as things become more concrete.",
                "Eric: Understood.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 43 - Spain conversation screenshot F",
            [
                "Eric: Can you give me an update on the topic of Spain?",
                "Natalie: You mentioned you would comment on it in a week, so I am just asking.",
                "Eric: Yes, this would work as a topic to revisit once the details are clearer.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 44 - Spain conversation screenshot G",
            [
                "Natalie: When I said I might have to leave Canada this summer, I meant I might need to live in Spain.",
                "Eric: I can see that Ava is very attached to you and I believe it is better for her if she stays with you.",
                "Eric: For that reason I am ready to let you go live in Spain, subject to conditions that make sense for Ava.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 45 - Poem shared in chat, part 1",
            [
                "Eric: Wrote something for her, sending it to you.",
                "Eric: We were walking home from the metro in the rain, just the two of us.",
                "Eric: She didn't like the rain at first, kept complaining about her shoes.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 46 - Poem shared in chat, part 2",
            [
                "Eric: Then she saw a puddle and forgot all about it.",
                "Eric: I was watching her more than the road, honestly.",
                "Eric: She got upset for a second outside the station, I think she was just tired.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 47 - Poem shared in chat, part 3",
            [
                "Eric: I gave her the umbrella to hold, just to give her something to do with her hands.",
                "Eric: The button on it made her laugh, she pressed it about ten times.",
                "Eric: That was it, she was fine again.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 48 - Poem shared in chat, part 4",
            [
                "Eric: She carried it herself the rest of the way, first time she's done that.",
                "Eric: Kept telling me she didn't need help.",
                "Natalie: That sounds like her.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 49 - Poem shared in chat, part 5",
            [
                "Eric: I was just watching the umbrella bob along the sidewalk the whole way.",
                "Eric: Had to keep reminding her to stop at the crossings.",
                "Eric: She listened, mostly.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 50 - Poem shared in chat, part 6",
            [
                "Eric: We got home right as it let up.",
                "Eric: You were at the door when we got there.",
                "Eric: Small thing, but I wanted you to have it too.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 51 - Hearing minutes discussed in chat, May 15 2026 (part 1)",
            [
                "Natalie: Filed notice today that I intend to seek a safeguard order.",
                "Natalie: And ask for permission to relocate with Ava to Spain.",
                "Eric: That's the first I'm hearing the word safeguard.",
            ],
            style="chat",
        ),
        PdfPage(
            "P14 Page 52 - Hearing minutes discussed in chat, May 15 2026 (part 2)",
            [
                "Natalie: The matter got postponed, they want more time to prepare.",
                "Natalie: There's a new sworn filing and exhibits due before the next date.",
                "Eric: Send me the new date when you have it.",
            ],
            style="chat",
        ),
    ]


def write_source_texts(p13_pages: list[PdfPage], p14_pages: list[PdfPage]) -> None:
    DEMO_PREP_DIR.mkdir(parents=True, exist_ok=True)
    (DEMO_PREP_DIR / "LIVE_06_email_body.txt").write_text(french_email_body())

    p13_fr = []
    for idx, page in enumerate(p13_pages, start=1):
        p13_fr.append(f"--- Page {idx}: {page.title} ---\n")
        p13_fr.extend(f"{paragraph}\n" for paragraph in page.paragraphs)
        p13_fr.append("\n")
    (DEMO_PREP_DIR / "P13_french_relocation_declaration.fr.txt").write_text("".join(p13_fr))
    (DEMO_PREP_DIR / "P13_french_relocation_declaration.en.review.txt").write_text(english_review_text())

    p14_text = []
    for idx, page in enumerate(p14_pages, start=1):
        p14_text.append(f"--- Page {idx}: {page.title} ---\n")
        p14_text.extend(f"{paragraph}\n" for paragraph in page.paragraphs)
        p14_text.append("\n")
    (DEMO_PREP_DIR / "P14_english_message_exhibit.txt").write_text("".join(p14_text))


def build_email(p13_bytes: bytes, p14_bytes: bytes) -> bytes:
    msg = EmailMessage(policy=SMTP)
    msg["Date"] = LIVE_EMAIL_DATE
    msg["From"] = "Dana Cho <dcho@chofamilylaw.example>"
    msg["To"] = "Natalie Brooks <natalie.brooks@example.com>"
    msg["Cc"] = "Olivia Martin <omartin@chofamilylaw.example>"
    msg["Subject"] = LIVE_EMAIL_SUBJECT
    msg["Message-ID"] = "<178521101305.2340.21984546721189123456.dlyte-live-6@demo.example>"
    msg["X-DLYTE-Classification"] = "Relevant and urgent"
    msg["X-DLYTE-Thread"] = "Court filing"
    msg["X-DLYTE-Synthetic"] = "true"
    msg.set_content(french_email_body())
    msg.add_attachment(p13_bytes, maintype="application", subtype="pdf", filename=P13_FILENAME)
    msg.add_attachment(p14_bytes, maintype="application", subtype="pdf", filename=P14_FILENAME)
    return msg.as_bytes()


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2))


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_json(path: Path):
    return json.loads(path.read_text())


def update_manifests(p13_pdf: Path, p14_pdf: Path, live_email: Path) -> None:
    p13_bytes = p13_pdf.read_bytes()
    p14_bytes = p14_pdf.read_bytes()
    email_bytes = live_email.read_bytes()

    # emails_manifest
    emails_manifest = load_json(MANIFEST_DIR / "emails_manifest.json")
    emails_manifest.append(
        {
            "batch": "live",
            "seq": 6,
            "filename": LIVE_EMAIL_FILENAME,
            "date": LIVE_EMAIL_DATE,
            "from": "Dana Cho <dcho@chofamilylaw.example>",
            "to": "Natalie Brooks <natalie.brooks@example.com>",
            "cc": "Olivia Martin <omartin@chofamilylaw.example>",
            "subject": LIVE_EMAIL_SUBJECT,
            "classification": "Relevant and urgent",
            "thread": "Court filing",
            "urgent": "yes",
            "attachments": f"{P13_FILENAME}; {P14_FILENAME}",
            "deadlines": "",
            "expected": "Use translation on the French declaration; treat the Spain relocation request as a party filing supported by P14, not an order.",
        }
    )
    write_json(MANIFEST_DIR / "emails_manifest.json", emails_manifest)
    write_csv(
        MANIFEST_DIR / "emails_manifest.csv",
        emails_manifest,
        [
            "batch",
            "seq",
            "filename",
            "date",
            "from",
            "to",
            "cc",
            "subject",
            "classification",
            "thread",
            "urgent",
            "attachments",
            "deadlines",
            "expected",
        ],
    )

    # attachment mapping
    attachment_mapping = load_json(MANIFEST_DIR / "attachment_mapping.json")
    attachment_mapping.extend(
        [
            {
                "batch": "live",
                "email_filename": LIVE_EMAIL_FILENAME,
                "email_subject": LIVE_EMAIL_SUBJECT,
                "attachment_filename": P13_FILENAME,
                "source_folder": "Live_Attachments",
                "sha256": sha256_bytes(p13_bytes),
            },
            {
                "batch": "live",
                "email_filename": LIVE_EMAIL_FILENAME,
                "email_subject": LIVE_EMAIL_SUBJECT,
                "attachment_filename": P14_FILENAME,
                "source_folder": "Live_Attachments",
                "sha256": sha256_bytes(p14_bytes),
            },
        ]
    )
    write_json(MANIFEST_DIR / "attachment_mapping.json", attachment_mapping)
    write_csv(
        MANIFEST_DIR / "attachment_mapping.csv",
        attachment_mapping,
        ["batch", "email_filename", "email_subject", "attachment_filename", "source_folder", "sha256"],
    )

    # pdf inventory
    pdf_inventory = load_json(MANIFEST_DIR / "pdf_inventory.json")
    pdf_inventory.extend(
        [
            {
                "category": "live",
                "filename": P13_FILENAME,
                "pages": 8,
                "bytes": len(p13_bytes),
                "sha256": sha256_bytes(p13_bytes),
            },
            {
                "category": "live",
                "filename": P14_FILENAME,
                "pages": 52,
                "bytes": len(p14_bytes),
                "sha256": sha256_bytes(p14_bytes),
            },
        ]
    )
    write_json(MANIFEST_DIR / "pdf_inventory.json", pdf_inventory)
    write_csv(MANIFEST_DIR / "pdf_inventory.csv", pdf_inventory, ["category", "filename", "pages", "bytes", "sha256"])

    # chronology
    chronology = load_json(MANIFEST_DIR / "expected_chronology.json")
    chronology.append(
        {
            "date": "2026-04-28",
            "event": "Dana sends Natalie a French draft relocation declaration and a 52-page English exhibit packet supporting a requested return to Spain before the September 2026 school year.",
            "status": "party request under review; no relocation order entered",
            "sources": "LIVE 6; P13; P14",
        }
    )
    write_json(MANIFEST_DIR / "expected_chronology.json", chronology)
    with (MANIFEST_DIR / "expected_chronology.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "event", "status", "sources"])
        writer.writeheader()
        writer.writerows(chronology)

    # state transitions
    state_transitions = load_json(MANIFEST_DIR / "expected_state_transitions.json")
    state_transitions.append(
        {
            "stage": "After LIVE_06",
            "change": "Add a Spain relocation request packet supported by a French declaration (P13) and a 52-page English exhibit packet (P14). Treat it as a party filing only; existing orders and hearing dates remain active unless the court changes them.",
        }
    )
    write_json(MANIFEST_DIR / "expected_state_transitions.json", state_transitions)

    # demo queries
    demo_queries = load_json(MANIFEST_DIR / "expected_demo_queries.json")
    demo_queries.extend(
        [
            {
                "question": "What does LIVE_06 ask for?",
                "expected_answer": "A request to return to Spain with Ava before the September 2026 school year, supported by a French declaration and a 52-page English exhibit packet; it is a party request, not an order.",
            },
            {
                "question": "Which exhibit pages relate to whether Eric was open in writing to a Spain move?",
                "expected_answer": "The key written exchanges are on page 1 and pages 38-44 of P14, especially pages 41 and 44.",
            },
            {
                "question": "What is the key message in the Spain-consent exchanges in P14?",
                "expected_answer": "Eric writes that Ava is attached to Natalie, that he believes it is better for her to stay with Natalie, and that he is ready to let them live in Spain subject to conditions.",
            },
        ]
    )
    write_json(MANIFEST_DIR / "expected_demo_queries.json", demo_queries)

    # version map
    version_map = load_json(MANIFEST_DIR / "document_version_map.json")
    version_map.append(
        {
            "item": "Spain relocation request",
            "older_version": "No prior relocation filing in the baseline set",
            "newer_version": "April 28 French declaration and English exhibit packet (P13/P14)",
            "active": "pending party request only",
            "rule": "Treat as a new proposed request supported by exhibits, not as an order or agreed modification.",
        }
    )
    write_json(MANIFEST_DIR / "document_version_map.json", version_map)
    write_csv(
        MANIFEST_DIR / "document_version_map.csv",
        version_map,
        ["item", "older_version", "newer_version", "active", "rule"],
    )

    # confirmed/disputed/open issues
    issues = load_json(MANIFEST_DIR / "confirmed_disputed_open_issues.json")
    issues.append(
        {
            "issue": "Spain relocation request",
            "status": "newly asserted and unresolved",
            "answer": "Natalie seeks permission to return to Spain with Ava before the September 2026 school year; the request is supported by P13 and P14 but is not approved or active.",
            "sources": "LIVE 6; P13; P14",
        }
    )
    write_json(MANIFEST_DIR / "confirmed_disputed_open_issues.json", issues)
    write_csv(
        MANIFEST_DIR / "confirmed_disputed_open_issues.csv",
        issues,
        ["issue", "status", "answer", "sources"],
    )

    # README and reset instructions
    (PACKAGING_DIR / "README.txt").write_text(
        """DLYTE COMPLETE SYNTHETIC TEST PACKAGE
====================================

Purpose
-------
This package tests DLYTE as an AI-assisted family-law case organizer. It is designed to verify email and PDF import, relevance detection, OCR/search, chronology extraction, source linking, deadline tracking, document versioning, active-order identification, and confirmed-versus-disputed reasoning.

All people, addresses, courts, records, numbers, and events are fictional. Nothing in this package is legal advice or a real legal, medical, financial, school, or government record.

Contents
--------
- Historical_Emails: 25 baseline EML files.
- Live_Test_Emails: 6 later EML files to import one at a time.
- Core_PDFs: 8 central case documents embedded in historical emails.
- Live_Attachments: 6 later PDFs embedded in live emails.
- Supporting_Records: 26 identity, income, business, child, property, and communication PDFs.
- Manifests: expected classifications, chronology, deadlines, versions, issues, queries, mappings, inventory, checksums, and reset instructions.
- DLYTE_25_Historical_EMLs.zip and DLYTE_6_Live_EMLs.zip: convenience bundles.

Recommended test order
----------------------
1. Create a blank case.
2. Import Supporting_Records.
3. Import the 25 Historical_Emails.
4. Verify the baseline manifests.
5. Import LIVE_01 through LIVE_06 separately and verify each state transition.

Key baseline expectations
-------------------------
- P06 is the active temporary parenting order.
- Proposals and mediation records are historical positions or agreements, not controlling orders.
- Natalie current income is $74,000 base plus $6,500 bonus.
- Eric business income remains a disputed calculation even after tax records arrive.
- The $8,400 transfer is confirmed; complete authorization and tracing remain unresolved.
- Baseline court dates are May 15 and May 29, later replaced only by LIVE_05.
- LIVE_06 adds a Spain relocation request packet supported by a French declaration and a 52-page English exhibit packet. It is not an order.

Counts
------
- 25 historical emails
- 6 live-test emails
- 8 core PDFs
- 6 live attachment PDFs
- 26 supporting PDFs
- 40 unique PDFs total
- 14 PDF attachment instances embedded across the 31 emails
"""
    )
    (MANIFEST_DIR / "reset_reimport_instructions.txt").write_text(
        """RESET AND RE-IMPORT PROCEDURE

1. Delete the Brooks family DLYTE demo case/workspace from the application.
2. Confirm that indexed text, chronology events, extracted deadlines, source links, confirmed/disputed classifications, document versions, and cached chat answers were deleted with the case.
3. If Live_Test_Emails were sent through a connected test inbox, remove them from the monitored label/folder before rebuilding the baseline.
4. Create a new empty case named "Brooks Family-Law Demo".
5. Import Supporting_Records and the 25 Historical_Emails. Do not import Live_Test_Emails yet.
6. Verify the baseline against expected_state_transitions.json, confirmed_disputed_open_issues.json, document_version_map.csv, expected_chronology.csv, and expected_deadlines.csv.
7. Import LIVE_01 through LIVE_06 one at a time. After each refresh, verify the expected state transition before continuing.
8. Repeat until duplicate detection, relevance filtering, version control, active-order identification, deadline extraction, chronology, confirmed-versus-disputed classification, and source linking behave consistently.

Duplicate-detection option
--------------------------
Import Core_PDFs directly before importing Historical_Emails. Since relevant EML files embed the same PDFs, the application should recognize duplicates or present an explicit review state rather than silently creating contradictory copies.

Important baseline
------------------
The active parenting source after historical import is P06. P08 controls baseline dates. P12 changes only the settlement conference and status dates after LIVE_05. LIVE_06 adds a Spain relocation request packet but does not itself change active orders or dates.
"""
    )

    # convenience zip
    zip_path = PACKAGING_DIR / "DLYTE_6_Live_EMLs.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(LIVE_EMAILS_DIR.glob("*.eml")):
            zf.write(path, arcname=path.name)

    # checksums
    checksum_rows = []
    for path in sorted(DATA_DIR.rglob("*")):
        if not path.is_file() or path.name == ".DS_Store":
            continue
        relative = path.relative_to(DATA_DIR)
        checksum_rows.append(
            {
                "relative_path": str(relative).replace("\\", "/"),
                "sha256": sha256_bytes(path.read_bytes()),
                "bytes": path.stat().st_size,
            }
        )
    for path in sorted(MANIFEST_DIR.glob("*")):
        if path.is_file():
            checksum_rows.append(
                {
                    "relative_path": f"Manifests/{path.name}",
                    "sha256": sha256_bytes(path.read_bytes()),
                    "bytes": path.stat().st_size,
                }
            )
    for path in sorted(PACKAGING_DIR.glob("*")):
        if path.is_file() and path.name != ".DS_Store":
            checksum_rows.append(
                {
                    "relative_path": path.name,
                    "sha256": sha256_bytes(path.read_bytes()),
                    "bytes": path.stat().st_size,
                }
            )
    write_csv(MANIFEST_DIR / "checksums_sha256.csv", checksum_rows, ["relative_path", "sha256", "bytes"])


def main() -> None:
    p13_pages = french_filing_pages()
    p14_pages = exhibit_pages()
    write_source_texts(p13_pages, p14_pages)

    LIVE_ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    p13_bytes = build_pdf_bytes(P13_FILENAME, p13_pages)
    p14_bytes = build_pdf_bytes(P14_FILENAME, p14_pages)
    p13_path = LIVE_ATTACHMENTS_DIR / P13_FILENAME
    p14_path = LIVE_ATTACHMENTS_DIR / P14_FILENAME
    p13_path.write_bytes(p13_bytes)
    p14_path.write_bytes(p14_bytes)

    LIVE_EMAILS_DIR.mkdir(parents=True, exist_ok=True)
    live_email_path = LIVE_EMAILS_DIR / LIVE_EMAIL_FILENAME
    live_email_path.write_bytes(build_email(p13_bytes, p14_bytes))

    update_manifests(p13_path, p14_path, live_email_path)

    print(f"Wrote {p13_path.relative_to(APP_DIR)}")
    print(f"Wrote {p14_path.relative_to(APP_DIR)}")
    print(f"Wrote {live_email_path.relative_to(APP_DIR)}")


if __name__ == "__main__":
    main()
