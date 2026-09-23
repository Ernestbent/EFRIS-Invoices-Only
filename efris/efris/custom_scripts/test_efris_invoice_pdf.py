from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from efris.efris.custom_scripts.efris_invoice_pdf import (
    EFRISInvoicePDFError,
    PDF_DOWNLOAD_TIMEOUT,
    PDF_REQUEST_HEADERS,
    URA_INVOICE_PDF_URLS,
    URA_PRODUCTION_HOST,
    URA_TEST_HOST,
    decode_ura_chunked_body,
    download_ura_invoice_pdf,
    enqueue_ura_invoice_pdf,
    extract_invoice_no,
    get_attachment_file_name,
    get_ura_invoice_pdf_url,
    save_ura_invoice_pdf,
)


class TestEFRISInvoicePDF(TestCase):
    def test_extracts_invoice_no_from_efris_fragment_url(self):
        qr_code = (
            "https://efris.ura.go.ug/site_mobile/#/invoiceValidation"
            "?invoiceNo=126901526041&antiFakeCode=26495386264197296796"
        )

        self.assertEqual(extract_invoice_no(qr_code), "126901526041")

    def test_extracts_invoice_no_from_html_escaped_url(self):
        qr_code = (
            "https://efris.ura.go.ug/site_mobile/#/invoiceValidation"
            "?invoiceNo=126901526041&amp;antiFakeCode=26495386264197296796"
        )

        self.assertEqual(extract_invoice_no(qr_code), "126901526041")

    @patch("efris.efris.custom_scripts.efris_invoice_pdf.requests.get")
    def test_downloads_pdf_from_fixed_ura_endpoint(self, get):
        response = Mock()
        response.headers = {"Content-Type": "application/pdf; charset=binary"}
        response.content = b"%PDF-1.4 official invoice"
        get.return_value = response

        pdf_url = URA_INVOICE_PDF_URLS[URA_PRODUCTION_HOST]
        content = download_ura_invoice_pdf("126901526041", pdf_url)

        self.assertEqual(content, response.content)
        get.assert_called_once_with(
            pdf_url,
            params={"invoiceNo": "126901526041"},
            headers=PDF_REQUEST_HEADERS,
            timeout=PDF_DOWNLOAD_TIMEOUT,
        )
        response.raise_for_status.assert_called_once_with()

    @patch("efris.efris.custom_scripts.efris_invoice_pdf.requests.get")
    def test_rejects_non_pdf_response(self, get):
        response = Mock()
        response.headers = {"Content-Type": "text/html"}
        response.content = b"<html>URA error</html>"
        get.return_value = response

        with self.assertRaises(EFRISInvoicePDFError):
            download_ura_invoice_pdf(
                "126901526041",
                URA_INVOICE_PDF_URLS[URA_PRODUCTION_HOST],
            )

    @patch("efris.efris.custom_scripts.efris_invoice_pdf.requests.get")
    def test_decodes_ura_nested_chunked_pdf_response(self, get):
        pdf_content = b"%PDF-1.4 official invoice\n%%EOF\n"
        response = Mock()
        response.headers = {}
        response.content = (
            f"{len(pdf_content):x}\r\n".encode()
            + pdf_content
            + b"\r\n0\r\n\r\n"
        )
        get.return_value = response

        content = download_ura_invoice_pdf(
            "326044246564",
            URA_INVOICE_PDF_URLS[URA_TEST_HOST],
        )

        self.assertEqual(content, pdf_content)

    def test_leaves_normal_pdf_body_unchanged(self):
        pdf_content = b"%PDF-1.4 official invoice\n%%EOF\n"

        self.assertEqual(decode_ura_chunked_body(pdf_content), pdf_content)

    def test_resolves_test_pdf_endpoint_from_qr_host(self):
        qr_code = (
            "https://efristest.ura.go.ug/site_new/#/invoiceValidation"
            "?invoiceNo=326044246564&antiFakeCode=37889904523753239164"
        )

        self.assertEqual(
            get_ura_invoice_pdf_url(qr_code),
            URA_INVOICE_PDF_URLS[URA_TEST_HOST],
        )

    def test_rejects_unknown_qr_host(self):
        with self.assertRaises(EFRISInvoicePDFError):
            get_ura_invoice_pdf_url(
                "https://example.com/#/invoiceValidation?invoiceNo=126901526041"
            )

    @patch("efris.efris.custom_scripts.efris_invoice_pdf.save_file")
    @patch("efris.efris.custom_scripts.efris_invoice_pdf.download_ura_invoice_pdf")
    @patch("efris.efris.custom_scripts.efris_invoice_pdf.frappe")
    def test_attaches_downloaded_pdf_privately(self, frappe, download, save):
        invoice = SimpleNamespace(
            name="SINV-0001",
            doctype="Sales Invoice",
            get=lambda fieldname: {
                "custom_qr_code": (
                    "https://efris.ura.go.ug/site_mobile/#/invoiceValidation"
                    "?invoiceNo=126901526041&antiFakeCode=26495386264197296796"
                ),
                "custom_fdn": "126901526041",
            }.get(fieldname),
        )
        attached_file = SimpleNamespace(
            name="file-record-name",
            file_name="SINV-0001-EFRIS.pdf",
            file_url="/private/files/SINV-0001-EFRIS.pdf",
            is_private=1,
        )
        frappe.get_all.return_value = []
        frappe.get_doc.return_value = invoice
        download.return_value = b"%PDF-1.4 official invoice"
        save.return_value = attached_file

        result = save_ura_invoice_pdf(invoice.name)

        self.assertIs(result, attached_file)
        download.assert_called_once_with(
            "126901526041",
            URA_INVOICE_PDF_URLS[URA_PRODUCTION_HOST],
        )
        save.assert_called_once_with(
            "SINV-0001-EFRIS.pdf",
            b"%PDF-1.4 official invoice",
            "Sales Invoice",
            "SINV-0001",
            is_private=1,
        )
        frappe.publish_realtime.assert_called_once_with(
            "efris_invoice_pdf_attached",
            {
                "invoice_name": "SINV-0001",
                "attachment": {
                    "name": "file-record-name",
                    "file_name": "SINV-0001-EFRIS.pdf",
                    "file_url": "/private/files/SINV-0001-EFRIS.pdf",
                    "is_private": 1,
                },
            },
            doctype="Sales Invoice",
            docname="SINV-0001",
            after_commit=True,
        )

    @patch("efris.efris.custom_scripts.efris_invoice_pdf.download_ura_invoice_pdf")
    @patch("efris.efris.custom_scripts.efris_invoice_pdf.frappe")
    def test_recognizes_hash_suffixed_existing_attachment(self, frappe, download):
        invoice = SimpleNamespace(
            name="SINV-0001",
            doctype="Sales Invoice",
            get=lambda fieldname: {
                "custom_qr_code": (
                    "https://efris.ura.go.ug/site_mobile/#/invoiceValidation"
                    "?invoiceNo=126901526041&antiFakeCode=26495386264197296796"
                ),
                "custom_fdn": "126901526041",
            }.get(fieldname),
        )
        attached_file = Mock()
        frappe.get_doc.side_effect = [invoice, attached_file]
        frappe.get_all.return_value = [
            {
                "name": "file-record-name",
                "file_name": "SINV-0001-EFRIS107d25.pdf",
            }
        ]

        result = save_ura_invoice_pdf(invoice.name)

        self.assertIs(result, attached_file)
        frappe.get_doc.assert_any_call("File", "file-record-name")
        download.assert_not_called()

    def test_sanitizes_attachment_file_name(self):
        self.assertEqual(
            get_attachment_file_name("SINV/2026 0001"),
            "SINV-2026-0001-EFRIS.pdf",
        )

    @patch("efris.efris.custom_scripts.efris_invoice_pdf.frappe")
    def test_queues_worker_immediately_after_existing_commit(self, frappe):
        enqueue_ura_invoice_pdf("SINV-0001")

        frappe.enqueue.assert_called_once_with(
            "efris.efris.custom_scripts.efris_invoice_pdf.save_ura_invoice_pdf",
            inv_name="SINV-0001",
            queue="default",
            at_front=True,
            job_id="efris-ura-pdf::SINV-0001",
            deduplicate=True,
        )
