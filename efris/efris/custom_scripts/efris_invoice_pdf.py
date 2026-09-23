import html
import re
from urllib.parse import parse_qs, urlsplit

import frappe
import requests
from frappe.utils.file_manager import save_file


URA_PRODUCTION_HOST = "efris.ura.go.ug"
URA_TEST_HOST = "efristest.ura.go.ug"
URA_INVOICE_PDF_URLS = {
    URA_PRODUCTION_HOST: "https://efris.ura.go.ug/aos/mis/inquiry/view",
    URA_TEST_HOST: "https://efristest.ura.go.ug/efrissite/aos/mis/inquiry/view",
}
PDF_DOWNLOAD_TIMEOUT = (5, 15)
PDF_CONTENT_TYPE = "application/pdf"
PDF_SIGNATURE = b"%PDF"
PDF_REQUEST_HEADERS = {
    "Accept": PDF_CONTENT_TYPE,
    "Accept-Encoding": "identity",
    "Connection": "close",
    "User-Agent": "Mozilla/5.0",
}


class EFRISInvoicePDFError(Exception):
    pass


def extract_invoice_no(qr_code):
    """Return invoiceNo from the EFRIS QR URL, whose query is inside the URL fragment."""
    qr_code = html.unescape(str(qr_code or "").strip())
    if not qr_code:
        return ""

    parsed_url = urlsplit(qr_code)
    query_strings = [parsed_url.query]

    if parsed_url.fragment:
        query_strings.append(urlsplit(parsed_url.fragment).query)

    for query_string in query_strings:
        invoice_numbers = parse_qs(query_string).get("invoiceNo") or []
        if invoice_numbers and invoice_numbers[0].strip():
            return invoice_numbers[0].strip()

    return ""


def get_efris_invoice_no(invoice):
    invoice_no = extract_invoice_no(invoice.get("custom_qr_code"))
    if not invoice_no:
        invoice_no = str(invoice.get("custom_fdn") or "").strip()

    if not invoice_no:
        raise EFRISInvoicePDFError(
            f"Sales Invoice {invoice.name} has no EFRIS invoice number or QR code."
        )

    return invoice_no


def get_ura_invoice_pdf_url(qr_code):
    qr_code = html.unescape(str(qr_code or "").strip())
    host = (urlsplit(qr_code).hostname or "").lower()
    pdf_url = URA_INVOICE_PDF_URLS.get(host)

    if not pdf_url:
        raise EFRISInvoicePDFError(
            f"Unsupported EFRIS QR host: {host or 'missing host'}."
        )

    return pdf_url


def get_attachment_file_name(invoice_name):
    safe_invoice_name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(invoice_name)).strip("-.")
    safe_invoice_name = safe_invoice_name or "Sales-Invoice"
    return f"{safe_invoice_name}-EFRIS.pdf"


def decode_ura_chunked_body(content):
    """Decode the extra HTTP chunk framing returned by URA when keep-alive is disabled."""
    if content.startswith(PDF_SIGNATURE):
        return content

    decoded = bytearray()
    offset = 0

    try:
        while offset < len(content):
            line_end = content.index(b"\r\n", offset)
            size_text = content[offset:line_end].split(b";", 1)[0]
            chunk_size = int(size_text, 16)
            offset = line_end + 2

            if chunk_size == 0:
                return bytes(decoded)

            chunk_end = offset + chunk_size
            if chunk_end > len(content) or content[chunk_end:chunk_end + 2] != b"\r\n":
                return content

            decoded.extend(content[offset:chunk_end])
            offset = chunk_end + 2
    except (ValueError, IndexError):
        return content

    return content


def download_ura_invoice_pdf(invoice_no, pdf_url):
    response = requests.get(
        pdf_url,
        params={"invoiceNo": invoice_no},
        headers=PDF_REQUEST_HEADERS,
        timeout=PDF_DOWNLOAD_TIMEOUT,
    )
    response.raise_for_status()

    pdf_content = decode_ura_chunked_body(response.content)
    content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if (content_type and content_type != PDF_CONTENT_TYPE) or not pdf_content.startswith(PDF_SIGNATURE):
        raise EFRISInvoicePDFError(
            f"URA did not return a valid PDF for EFRIS invoice {invoice_no}."
        )

    return pdf_content


def save_ura_invoice_pdf(inv_name):
    """Download URA's official fiscal invoice PDF and attach it to a Sales Invoice."""
    invoice = frappe.get_doc("Sales Invoice", inv_name)
    file_name = get_attachment_file_name(invoice.name)
    file_filters = {
        "attached_to_doctype": invoice.doctype,
        "attached_to_name": invoice.name,
    }

    file_name_stem = file_name.removesuffix(".pdf")
    existing_files = frappe.get_all(
        "File",
        filters=file_filters,
        fields=["name", "file_name"],
    )
    for existing_file in existing_files:
        existing_file_name = str(existing_file.get("file_name") or "")
        if existing_file_name == file_name or (
            existing_file_name.startswith(file_name_stem)
            and existing_file_name.lower().endswith(".pdf")
        ):
            return frappe.get_doc("File", existing_file.get("name"))

    invoice_no = get_efris_invoice_no(invoice)
    pdf_url = get_ura_invoice_pdf_url(invoice.get("custom_qr_code"))
    pdf_content = download_ura_invoice_pdf(invoice_no, pdf_url)

    file_doc = save_file(
        file_name,
        pdf_content,
        invoice.doctype,
        invoice.name,
        is_private=1,
    )

    frappe.publish_realtime(
        "efris_invoice_pdf_attached",
        {
            "invoice_name": invoice.name,
            "attachment": {
                "name": file_doc.name,
                "file_name": file_doc.file_name,
                "file_url": file_doc.file_url,
                "is_private": file_doc.is_private,
            },
        },
        doctype=invoice.doctype,
        docname=invoice.name,
        after_commit=True,
    )

    return file_doc


def enqueue_ura_invoice_pdf(inv_name):
    """Queue attachment creation after upload_invoice has committed the EFRIS fields."""
    return frappe.enqueue(
        "efris.efris.custom_scripts.efris_invoice_pdf.save_ura_invoice_pdf",
        inv_name=inv_name,
        queue="default",
        at_front=True,
        job_id=f"efris-ura-pdf::{inv_name}",
        deduplicate=True,
    )
