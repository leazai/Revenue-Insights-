"""
Income Statement Processing Service for Railway
Receives Income Statement CSV from Mailgun and sends processed data to Lovable webhook

SECURITY: Mailgun signature verification with HMAC-SHA256
PERFORMANCE: Fast response (<1s) with background CSV processing
RELIABILITY: Robust error handling and logging

Based on learnings from Master Data service implementation.
"""

import os
import io
import hmac
import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import pandas as pd
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="Income Statement Processing Service")

# Environment variables (strip whitespace to handle hidden newlines from Railway UI)
LOVABLE_WEBHOOK_URL = os.getenv("LOVABLE_WEBHOOK_URL", "").strip()
INCOME_STATEMENT_WEBHOOK_TOKEN = os.getenv("INCOME_STATEMENT_WEBHOOK_TOKEN", "").strip()
MAILGUN_WEBHOOK_SECRET = os.getenv("MAILGUN_WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", 8000))

# Validate critical environment variables
if not LOVABLE_WEBHOOK_URL:
    logger.error("❌ LOVABLE_WEBHOOK_URL not set")
if not INCOME_STATEMENT_WEBHOOK_TOKEN:
    logger.error("❌ INCOME_STATEMENT_WEBHOOK_TOKEN not set")
if not MAILGUN_WEBHOOK_SECRET:
    logger.warning("⚠️  MAILGUN_WEBHOOK_SECRET not set - signature verification will be skipped")

# Processing stats
processing_stats = {
    'last_processed': None,
    'total_processed': 0,
    'last_error': None,
    'last_filename': None
}


def verify_mailgun_signature(token: str, timestamp: str, signature: str) -> bool:
    """
    Verify Mailgun webhook signature for security.
    Uses HMAC-SHA256 to verify the request came from Mailgun.
    
    Args:
        token: Random token from Mailgun
        timestamp: Unix timestamp from Mailgun
        signature: HMAC signature from Mailgun
        
    Returns:
        bool: True if signature is valid, False otherwise
    """
    if not MAILGUN_WEBHOOK_SECRET:
        logger.warning("MAILGUN_WEBHOOK_SECRET not set, skipping verification")
        return True
    
    try:
        # Compute HMAC-SHA256
        hmac_digest = hmac.new(
            key=MAILGUN_WEBHOOK_SECRET.encode('utf-8'),
            msg=f"{timestamp}{token}".encode('utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest()
        
        # Constant-time comparison to prevent timing attacks
        is_valid = hmac.compare_digest(signature, hmac_digest)
        
        if is_valid:
            logger.info("✅ Mailgun signature verified")
        else:
            logger.error("❌ Invalid Mailgun signature")
        
        return is_valid
    
    except Exception as e:
        logger.error(f"Error verifying Mailgun signature: {e}")
        return False


def safe_float(val) -> float:
    """
    Safely convert value to float, handling currency formatting.
    Handles: $1,234.56, (1234.56), empty strings, NaN
    
    Args:
        val: Value to convert (can be string, int, float, or NaN)
        
    Returns:
        float: Converted value or 0.0 if conversion fails
    """
    if pd.isna(val):
        return 0.0
    
    if isinstance(val, (int, float)):
        return float(val)
    
    if isinstance(val, str):
        # Remove $, commas, and whitespace
        val = val.replace('$', '').replace(',', '').strip()
        
        if val == '' or val == '-':
            return 0.0
        
        # Handle parentheses for negative numbers (accounting format)
        if val.startswith('(') and val.endswith(')'):
            val = '-' + val[1:-1]
    
    try:
        return float(val)
    except (ValueError, TypeError):
        logger.warning(f"Could not convert value to float: {val}")
        return 0.0


def detect_category_level(account_name: str, original_text: str) -> int:
    """
    Detect category hierarchy level based on indentation or naming patterns.
    
    Level 0: Top-level sections (e.g., "Operating Income", "Total Income")
    Level 1: Major categories (e.g., "MARKETING EXPENSE", "PAYROLL EXPENSE")
    Level 2: Subcategories (e.g., "Marketing - Advertising")
    Level 3: Line items (e.g., "Marketing - Advertising - Google Ads")
    
    Args:
        account_name: Cleaned account name
        original_text: Original text with indentation preserved
        
    Returns:
        int: Category level (0-3)
    """
    if pd.isna(account_name):
        return 0
    
    # Count leading spaces (if CSV preserves indentation)
    leading_spaces = len(original_text) - len(original_text.lstrip())
    
    if leading_spaces >= 12:
        return 3
    elif leading_spaces >= 8:
        return 2
    elif leading_spaces >= 4:
        return 1
    
    # Pattern-based detection
    account_str = str(account_name).strip()
    
    # Level 0: Major totals and sections
    if any(keyword in account_str for keyword in [
        'Total Income', 'Total Expense', 'Net Income', 'NOI', 
        'Net Operating Income', 'Operating Income & Expense'
    ]):
        return 0
    
    # Level 1: All caps categories
    if account_str.isupper() and len(account_str.split()) <= 4:
        return 1
    
    # Level 1: Starts with "Total "
    if account_str.startswith('Total '):
        return 1
    
    # Level 2: Contains dash or colon
    if ' - ' in account_str or ': ' in account_str:
        return 2
    
    # Default to level 2 (line item)
    return 2


def detect_category_type(account_name: str, row_index: int, total_rows: int) -> str:
    """
    Detect if category is income, expense, COGS, or other.
    
    Args:
        account_name: Account name to analyze
        row_index: Current row index
        total_rows: Total number of rows
        
    Returns:
        str: Category type ('income', 'expense', 'cogs', 'other')
    """
    if pd.isna(account_name):
        return 'other'
    
    account_str = str(account_name).lower()
    
    # Income indicators
    if any(keyword in account_str for keyword in [
        'income', 'revenue', 'fee income', 'management fee', 'leasing fee',
        'operating income', 'rental income'
    ]):
        return 'income'
    
    # COGS indicators
    if any(keyword in account_str for keyword in [
        'cogs', 'cost of goods', 'commission', 'direct cost', 'cost of sales'
    ]):
        return 'cogs'
    
    # Expense indicators
    if any(keyword in account_str for keyword in [
        'expense', 'cost', 'payroll', 'marketing', 'administrative', 
        'travel', 'insurance', 'office', 'bank', 'legal', 'professional'
    ]):
        return 'expense'
    
    # Position-based detection (income usually at top, expenses in middle)
    if row_index < total_rows * 0.2:
        return 'income'
    elif row_index < total_rows * 0.4:
        return 'cogs'
    elif row_index < total_rows * 0.9:
        return 'expense'
    else:
        return 'other'


def extract_parent_category(category_level: int, previous_categories: List[Dict]) -> Optional[str]:
    """
    Extract parent category based on hierarchy.
    Looks for the most recent category at level-1.
    
    Args:
        category_level: Current category level
        previous_categories: List of previously processed categories
        
    Returns:
        Optional[str]: Parent category name or None if top-level
    """
    if category_level == 0:
        return None
    
    # Find the most recent category at level-1
    for cat in reversed(previous_categories):
        if cat['category_level'] == category_level - 1:
            return cat['account_name']
    
    return None


def parse_income_statement_csv(csv_content: bytes) -> Dict:
    """
    Parse income statement CSV and extract structured data.
    
    The CSV has a hierarchical structure with:
    - Account Name column
    - 12 monthly columns (Jan-Dec)
    - Optional Total column
    
    Returns structured data with:
    - metadata: Report period, upload date, etc.
    - categories: Hierarchical category structure
    - monthly_data: Monthly values for each category
    - totals: Calculated key financial metrics
    
    Args:
        csv_content: Raw CSV bytes
        
    Returns:
        Dict with parsed data
    """
    logger.info("📊 Parsing income statement CSV...")
    
    # Read CSV
    df = pd.read_csv(io.BytesIO(csv_content), low_memory=False)
    logger.info(f"CSV loaded: {len(df)} rows, {len(df.columns)} columns")
    
    # Identify columns
    columns = df.columns.tolist()
    account_col = columns[0]  # First column is Account Name
    
    # Identify month columns (exclude Account Name and any Total column)
    month_columns = []
    for col in columns[1:]:
        col_lower = str(col).lower()
        if 'total' not in col_lower and col_lower != 'nan':
            month_columns.append(col)
    
    logger.info(f"Month columns detected: {month_columns}")
    
    if len(month_columns) != 12:
        logger.warning(f"Expected 12 month columns, found {len(month_columns)}")
    
    # Extract report period from column names
    report_period = None
    period_start = None
    period_end = None
    
    if month_columns:
        # Extract year from first month column (e.g., "Jan 2025" -> "2025")
        first_month = month_columns[0]
        year_match = re.search(r'20\d{2}', first_month)
        if year_match:
            report_period = year_match.group(0)
            
            # Parse period start and end
            try:
                first_month_dt = datetime.strptime(first_month, "%b %Y")
                period_start = first_month_dt.strftime("%Y-%m-01")
                
                last_month = month_columns[-1]
                last_month_dt = datetime.strptime(last_month, "%b %Y")
                
                # Last day of last month
                if last_month_dt.month == 12:
                    period_end = f"{last_month_dt.year}-12-31"
                else:
                    next_month = last_month_dt.replace(month=last_month_dt.month + 1, day=1)
                    period_end = (next_month - timedelta(days=1)).strftime("%Y-%m-%d")
            except Exception as e:
                logger.warning(f"Could not parse period dates: {e}")
    
    # Default to current year if not found
    if not report_period:
        report_period = datetime.now().strftime("%Y")
        period_start = f"{report_period}-01-01"
        period_end = f"{report_period}-12-31"
    
    # Process each row
    categories = []
    monthly_data = []
    previous_categories = []
    
    for idx, row in df.iterrows():
        account_name_raw = row.get(account_col)
        
        # Skip empty rows
        if pd.isna(account_name_raw) or str(account_name_raw).strip() == '':
            continue
        
        account_name = str(account_name_raw).strip()
        
        # Detect category properties
        category_level = detect_category_level(account_name, str(account_name_raw))
        category_type = detect_category_type(account_name, idx, len(df))
        parent_category = extract_parent_category(category_level, previous_categories)
        
        # Check if this is a total row
        is_total = (
            account_name.lower().startswith('total ') or 
            'net income' in account_name.lower() or
            'noi' in account_name.lower()
        )
        
        # Create category record
        category_id = f"cat_{idx}"
        category_record = {
            'category_id': category_id,
            'account_name': account_name,
            'category_level': category_level,
            'category_type': category_type,
            'parent_category': parent_category,
            'is_total': is_total,
            'display_order': idx
        }
        categories.append(category_record)
        previous_categories.append(category_record)
        
        # Extract monthly values
        for month_col in month_columns:
            amount = safe_float(row.get(month_col, 0))
            
            monthly_data.append({
                'category_id': category_id,
                'account_name': account_name,
                'month_year': month_col,
                'amount': amount
            })
    
    # Calculate key totals
    totals = calculate_totals(df, account_col, month_columns)
    
    # Build metadata
    metadata = {
        'report_period': report_period,
        'period_start': period_start,
        'period_end': period_end,
        'upload_date': datetime.now().isoformat(),
        'total_categories': len(categories),
        'total_data_points': len(monthly_data),
        'month_columns': month_columns
    }
    
    logger.info(f"✅ Parsed {len(categories)} categories, {len(monthly_data)} monthly data points")
    logger.info(f"Key totals: {totals}")
    
    return {
        'metadata': metadata,
        'categories': categories,
        'monthly_data': monthly_data,
        'totals': totals
    }


def calculate_totals(df: pd.DataFrame, account_col: str, month_columns: List[str]) -> Dict:
    """
    Calculate key financial totals from the income statement.
    
    Args:
        df: DataFrame with income statement data
        account_col: Name of the account column
        month_columns: List of month column names
        
    Returns:
        Dict with calculated totals
    """
    logger.info("🧮 Calculating financial totals...")
    
    def find_row_total(keyword: str) -> float:
        """Find row containing keyword and sum across all months"""
        matching_rows = df[df[account_col].str.contains(keyword, case=False, na=False, regex=True)]
        if matching_rows.empty:
            return 0.0
        
        # Use first matching row
        row = matching_rows.iloc[0]
        
        # Sum all month columns
        total = 0.0
        for month_col in month_columns:
            total += safe_float(row.get(month_col, 0))
        
        return total
    
    # Extract key totals
    total_operating_income = find_row_total(r'Total Operating Income')
    total_cogs = find_row_total(r'Total.*COGS|Total.*Cost of Goods')
    total_operating_expense = find_row_total(r'Total Operating Expense')
    noi = find_row_total(r'NOI|Net Operating Income')
    total_income = find_row_total(r'^Total Income$')
    total_expense = find_row_total(r'^Total Expense$')
    net_income = find_row_total(r'Net Income')
    
    # Calculate Real Revenue (Total Operating Income - Total COGS)
    real_revenue = total_operating_income - total_cogs
    
    totals = {
        'total_operating_income': round(total_operating_income, 2),
        'total_cogs': round(total_cogs, 2),
        'real_revenue': round(real_revenue, 2),
        'total_operating_expense': round(total_operating_expense, 2),
        'noi': round(noi, 2),
        'total_income': round(total_income, 2),
        'total_expense': round(total_expense, 2),
        'net_income': round(net_income, 2)
    }
    
    logger.info(f"Totals calculated: Real Revenue=${real_revenue:,.2f}, Net Income=${net_income:,.2f}")
    
    return totals


def send_to_lovable(parsed_data: Dict, batch_id: str) -> bool:
    """
    Send processed income statement data to Lovable webhook.
    
    Args:
        parsed_data: Parsed income statement data
        batch_id: Unique batch identifier
        
    Returns:
        bool: True if successful, False otherwise
    """
    if not LOVABLE_WEBHOOK_URL or not INCOME_STATEMENT_WEBHOOK_TOKEN:
        logger.error("❌ Lovable webhook URL or token not configured")
        return False
    
    # Flatten metadata fields to root level for Lovable edge function
    metadata = parsed_data['metadata']
    payload = {
        'batch_id': batch_id,
        'report_period': metadata.get('report_period'),
        'period_start': metadata.get('period_start'),
        'period_end': metadata.get('period_end'),
        'total_categories': metadata.get('total_categories'),
        'total_data_points': metadata.get('total_data_points'),
        'categories': parsed_data['categories'],
        'monthly_data': parsed_data['monthly_data'],
        'totals': parsed_data['totals']
    }
    
    headers = {
        'Authorization': f'Bearer {INCOME_STATEMENT_WEBHOOK_TOKEN}',
        'Content-Type': 'application/json'
    }
    
    try:
        logger.info(f"📤 Sending to Lovable: {LOVABLE_WEBHOOK_URL}")
        logger.info(f"Payload: {len(parsed_data['categories'])} categories, {len(parsed_data['monthly_data'])} data points")
        
        response = requests.post(
            LOVABLE_WEBHOOK_URL, 
            json=payload, 
            headers=headers, 
            timeout=60
        )
        response.raise_for_status()
        
        logger.info(f"✅ Successfully sent income statement data to Lovable (status: {response.status_code})")
        return True
    
    except requests.exceptions.Timeout:
        logger.error("❌ Timeout sending data to Lovable")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Failed to send data to Lovable: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response status: {e.response.status_code}")
            logger.error(f"Response body: {e.response.text[:500]}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error sending to Lovable: {e}")
        return False


def process_income_statement_background(csv_content: bytes, filename: str, batch_id: str):
    """
    Background task to process income statement CSV.
    This runs after responding to Mailgun to avoid timeout.
    
    Args:
        csv_content: Raw CSV bytes
        filename: Original filename
        batch_id: Unique batch identifier
    """
    try:
        logger.info(f"🔄 Starting income statement processing: {filename}")
        
        # Parse CSV (slow operation - done in background)
        parsed_data = parse_income_statement_csv(csv_content)
        
        # Send to Lovable
        success = send_to_lovable(parsed_data, batch_id)
        
        if success:
            processing_stats['last_processed'] = datetime.now().isoformat()
            processing_stats['total_processed'] += 1
            processing_stats['last_error'] = None
            processing_stats['last_filename'] = filename
            logger.info("✅ Income statement processing completed successfully")
        else:
            processing_stats['last_error'] = "Failed to send to Lovable"
            logger.error("❌ Failed to send data to Lovable")
    
    except Exception as e:
        error_msg = f"Error processing income statement: {str(e)}"
        logger.error(f"❌ {error_msg}", exc_info=True)
        processing_stats['last_error'] = error_msg


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "Income Statement Processing Service",
        "version": "2.0.0"
    }


@app.get("/status")
async def status():
    """Detailed status endpoint with configuration and stats"""
    return {
        "service": "Income Statement Processing Service",
        "version": "2.0.0",
        "status": "running",
        "config": {
            "lovable_webhook_configured": bool(LOVABLE_WEBHOOK_URL),
            "webhook_token_configured": bool(INCOME_STATEMENT_WEBHOOK_TOKEN),
            "mailgun_secret_configured": bool(MAILGUN_WEBHOOK_SECRET)
        },
        "stats": processing_stats
    }


@app.post("/webhook/mailgun")
async def mailgun_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receive income statement CSV from Mailgun.
    
    SECURITY: Verifies Mailgun signature with HMAC-SHA256
    PERFORMANCE: Responds in <1 second, processes CSV in background
    RELIABILITY: Robust error handling and logging
    
    Flow:
    1. Verify Mailgun signature (security)
    2. Extract CSV attachment (fast - just read bytes)
    3. Respond 200 OK immediately (<1s to avoid Mailgun timeout)
    4. Process CSV in background (slow - parsing, calculations, webhook call)
    """
    try:
        logger.info("📨 Received webhook from Mailgun")
        
        # Parse form data
        form_data = await request.form()
        
        # Verify Mailgun signature for security
        token = form_data.get("token", "")
        timestamp = form_data.get("timestamp", "")
        signature = form_data.get("signature", "")
        
        if not verify_mailgun_signature(token, timestamp, signature):
            logger.error("❌ Invalid Mailgun signature - rejecting request")
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        # Extract CSV attachment (read raw bytes only - no parsing yet)
        csv_content = None
        csv_filename = None
        
        for key, value in form_data.items():
            if key.startswith("attachment-") and hasattr(value, 'filename'):
                filename = value.filename.lower()
                
                # Look for income statement CSV
                if filename.endswith('.csv') and ('income' in filename or 'statement' in filename):
                    csv_content = await value.read()  # Read raw bytes (fast)
                    csv_filename = value.filename
                    logger.info(f"📎 Found CSV attachment: {csv_filename} ({len(csv_content)} bytes)")
                    break
        
        if not csv_content:
            logger.warning("⚠️  No income statement CSV found in attachments")
            return JSONResponse({
                "status": "success",
                "message": "No income statement CSV found in email"
            })
        
        # Generate batch ID
        batch_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Schedule background processing (CSV parsing happens here)
        background_tasks.add_task(
            process_income_statement_background,
            csv_content,
            csv_filename,
            batch_id
        )
        
        # RESPOND IMMEDIATELY - before any CSV parsing
        logger.info("✅ Responding 200 OK to Mailgun immediately, processing in background")
        
        return JSONResponse({
            "status": "success",
            "message": f"Income statement CSV received and queued for processing",
            "filename": csv_filename,
            "size_bytes": len(csv_content),
            "batch_id": batch_id,
            "timestamp": datetime.now().isoformat()
        })
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error processing Mailgun webhook: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ingest-income-statement")
async def ingest_income_statement(request: Request, background_tasks: BackgroundTasks):
    """
    Alternative endpoint for direct CSV upload (for testing).
    Accepts multipart/form-data with file upload.
    """
    try:
        logger.info("📨 Received direct CSV upload")
        
        # Parse form data
        form_data = await request.form()
        file = form_data.get("file")
        
        if not file or not hasattr(file, 'read'):
            raise HTTPException(status_code=400, detail="No file provided")
        
        # Read CSV content
        csv_content = await file.read()
        filename = file.filename
        
        logger.info(f"📎 Received file: {filename} ({len(csv_content)} bytes)")
        
        # Generate batch ID
        batch_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Schedule background processing
        background_tasks.add_task(
            process_income_statement_background,
            csv_content,
            filename,
            batch_id
        )
        
        return JSONResponse({
            "status": "success",
            "message": "Income statement CSV received and queued for processing",
            "filename": filename,
            "size_bytes": len(csv_content),
            "batch_id": batch_id
        })
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error in ingest endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    logger.info(f"🚀 Starting Income Statement Processing Service on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
