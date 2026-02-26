"""PDF Receipt generation service for GreenBay Market."""

import os
from datetime import datetime
from typing import Dict, Any, Optional
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from loguru import logger

from app.config import get_settings

settings = get_settings()


class ReceiptGenerator:
    """Service for generating PDF receipts."""
    
    def __init__(self):
        """Initialize receipt generator."""
        # Create receipts directory if it doesn't exist
        self.receipts_dir = os.path.join(os.getcwd(), "receipts")
        os.makedirs(self.receipts_dir, exist_ok=True)
    
    def generate_receipt(
        self,
        order_id: str,
        customer_phone: str,
        items: list[Dict[str, Any]],
        subtotal: float,
        delivery_cost: float,
        installation_cost: float = 0.0,
        total_amount: float = 0.0,
        delivery_address: str = "",
        delivery_timeline: str = "",
        payment_method: str = "M-Pesa",
        mpesa_transaction_id: str = "",
        customer_name: Optional[str] = None
    ) -> str:
        """
        Generate a PDF receipt for an order.
        
        Args:
            order_id: Order identifier
            customer_phone: Customer's phone number
            items: List of order items with product details
            subtotal: Items subtotal
            delivery_cost: Delivery cost
            installation_cost: Installation cost (optional)
            total_amount: Final total amount
            delivery_address: Delivery address
            delivery_timeline: Delivery timeline
            payment_method: Payment method used
            mpesa_transaction_id: M-Pesa transaction ID
            
        Returns:
            Path to generated PDF receipt
        """
        try:
            # Generate filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"receipt_{order_id}_{timestamp}.pdf"
            filepath = os.path.join(self.receipts_dir, filename)
            
            # Create PDF document
            doc = SimpleDocTemplate(
                filepath,
                pagesize=A4,
                rightMargin=72,
                leftMargin=72,
                topMargin=72,
                bottomMargin=18,
            )
            
            # Container for PDF elements
            elements = []
            styles = getSampleStyleSheet()
            
            # Custom styles
            title_style = ParagraphStyle(
                'CustomTitle',
                parent=styles['Heading1'],
                fontSize=24,
                textColor=colors.HexColor('#2E7D32'),
                spaceAfter=12,
                alignment=TA_CENTER
            )
            
            heading_style = ParagraphStyle(
                'CustomHeading',
                parent=styles['Heading2'],
                fontSize=14,
                textColor=colors.HexColor('#1B5E20'),
                spaceAfter=6,
                spaceBefore=12
            )
            
            # Title
            elements.append(Paragraph("GREENBAY MARKET", title_style))
            elements.append(Paragraph("Official Receipt", styles['Heading3']))
            elements.append(Spacer(1, 0.3 * inch))
            
            # Store Information
            store_info = f"""
            <b>GreenBay Market</b><br/>
            {settings.store_address}<br/>
            Phone: {settings.store_phone}<br/>
            Email: {settings.store_email}<br/>
            """
            elements.append(Paragraph(store_info, styles['Normal']))
            elements.append(Spacer(1, 0.2 * inch))
            
            # Receipt Details
            receipt_date = datetime.now().strftime("%B %d, %Y %I:%M %p")
            receipt_info = [
                ['Order ID:', order_id],
                ['Date:', receipt_date],
            ]
            
            # Add customer name if available
            if customer_name:
                receipt_info.append(['Customer Name:', customer_name])
            
            receipt_info.append(['Phone Number:', customer_phone])
            receipt_info.append(['Payment Method:', payment_method])
            
            if mpesa_transaction_id:
                receipt_info.append(['M-Pesa Ref:', mpesa_transaction_id])
            
            # Add delivery address if available
            if delivery_address:
                receipt_info.append(['Delivery Address:', delivery_address])
            
            receipt_table = Table(receipt_info, colWidths=[2*inch, 4*inch])
            receipt_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
                ('ALIGN', (1, 0), (1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ]))
            elements.append(receipt_table)
            elements.append(Spacer(1, 0.3 * inch))
            
            # Items Table
            elements.append(Paragraph("ORDER ITEMS", heading_style))
            
            # Table data - use Paragraph for product names to enable text wrapping
            items_data = [['Item', 'Qty', 'Price', 'Total']]
            for item in items:
                product_name = item.get('product_name', 'Unknown Product')
                # Wrap long product names
                product_para = Paragraph(product_name, styles['Normal'])
                items_data.append([
                    product_para,
                    str(item.get('quantity', 1)),
                    f"KES {item.get('price', 0):,.2f}",
                    f"KES {item.get('quantity', 1) * item.get('price', 0):,.2f}"
                ])
            
            # Adjusted column widths: wider Item column, narrower others
            items_table = Table(items_data, colWidths=[3.5*inch, 0.7*inch, 1.1*inch, 1.1*inch])
            items_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2E7D32')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
                ('ALIGN', (0, 1), (0, -1), 'LEFT'),  # Left align product names
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),  # Top align for wrapped text
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 12),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('TOPPADDING', (0, 1), (-1, -1), 6),  # Add padding for wrapped text
                ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
                ('FONTSIZE', (0, 1), (-1, -1), 10),
            ]))
            elements.append(items_table)
            elements.append(Spacer(1, 0.2 * inch))
            
            # Summary Table
            summary_data = [
                ['Subtotal:', f"KES {subtotal:,.2f}"],
                ['Delivery:', f"KES {delivery_cost:,.2f}"],
            ]
            
            if installation_cost > 0:
                summary_data.append(['Installation:', f"KES {installation_cost:,.2f}"])
            
            summary_data.append(['', ''])  # Spacer row
            summary_data.append(['TOTAL:', f"KES {total_amount:,.2f}"])
            
            summary_table = Table(summary_data, colWidths=[4.5*inch, 1.7*inch])
            summary_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, -1), (-1, -1), 14),
                ('TEXTCOLOR', (0, -1), (-1, -1), colors.HexColor('#2E7D32')),
                ('LINEABOVE', (0, -1), (-1, -1), 2, colors.HexColor('#2E7D32')),
                ('TOPPADDING', (0, -1), (-1, -1), 12),
                ('FONTSIZE', (0, 0), (-1, -3), 10),
            ]))
            elements.append(summary_table)
            elements.append(Spacer(1, 0.3 * inch))
            
            # Delivery Information (already shown in receipt details, but keep for clarity)
            if delivery_timeline and delivery_timeline != "pending":
                elements.append(Paragraph("DELIVERY INFORMATION", heading_style))
                delivery_info = f"""
                <b>Estimated Delivery:</b> {delivery_timeline}<br/>
                """
                elements.append(Paragraph(delivery_info, styles['Normal']))
                elements.append(Spacer(1, 0.2 * inch))
            
            # Footer
            footer_text = """
            <para align=center>
            <b>Thank you for shopping with GreenBay Market!</b><br/>
            Africa's premier marketplace for second-life, green-tech appliances.<br/>
            <br/>
            For support, contact: {phone} | {email}<br/>
            This is an electronically generated receipt and is valid without signature.
            </para>
            """.format(phone=settings.store_phone, email=settings.store_email)
            
            elements.append(Spacer(1, 0.3 * inch))
            elements.append(Paragraph(footer_text, styles['Normal']))
            
            # Build PDF
            doc.build(elements)
            
            logger.info(f"Receipt generated successfully: {filepath}")
            return filepath
            
        except Exception as e:
            logger.error(f"Error generating receipt: {e}")
            raise
    
    def get_receipt_url(self, filepath: str) -> str:
        """
        Get public URL for receipt (if hosted on web server).
        
        For now, returns local file path.
        In production, upload to S3 and return public URL.
        """
        # TODO: Upload to S3 and return public URL
        # For now, return relative path
        filename = os.path.basename(filepath)
        return f"https://greenbay.market/receipts/{filename}"


# Global service instance
receipt_generator = ReceiptGenerator()

