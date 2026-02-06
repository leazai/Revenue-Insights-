# Income Statement Processing Service

Railway microservice for processing AppFolio Income Statement CSV files via Mailgun webhooks.

## Features

- ✅ **Secure**: Mailgun signature verification with HMAC-SHA256
- ✅ **Fast**: Responds in <1 second to avoid Mailgun timeout
- ✅ **Reliable**: Background CSV processing with robust error handling
- ✅ **Production-ready**: Based on learnings from Master Data service

## Quick Start

### 1. Deploy to Railway

```bash
# Push to GitHub
git add .
git commit -m "Add income statement service"
git push

# Railway will auto-deploy
```

### 2. Configure Environment Variables

Set in Railway dashboard:

- `LOVABLE_WEBHOOK_URL` - Your Lovable edge function URL
- `INCOME_STATEMENT_WEBHOOK_TOKEN` - Secret token for authentication
- `MAILGUN_WEBHOOK_SECRET` - Mailgun webhook signing key

### 3. Configure Mailgun Route

- **Recipient**: `income-statement@your-domain.com`
- **Webhook URL**: `https://your-railway-url.railway.app/webhook/mailgun`
- **Method**: POST
- **Include attachments**: Yes

### 4. Test

Send email with CSV attachment to `income-statement@your-domain.com`

## API Endpoints

### `GET /`
Health check endpoint

**Response:**
```json
{
  "status": "healthy",
  "service": "Income Statement Processing Service",
  "version": "2.0.0"
}
```

### `GET /status`
Service status and configuration

**Response:**
```json
{
  "service": "Income Statement Processing Service",
  "version": "2.0.0",
  "status": "running",
  "config": {
    "lovable_webhook_configured": true,
    "webhook_token_configured": true,
    "mailgun_secret_configured": true
  },
  "stats": {
    "last_processed": "2026-02-06T14:30:22.123Z",
    "total_processed": 5,
    "last_error": null,
    "last_filename": "income_statement_12_month-20260206.csv"
  }
}
```

### `POST /webhook/mailgun`
Mailgun webhook endpoint (secured with signature verification)

**Request:** Mailgun form data with CSV attachment

**Response:**
```json
{
  "status": "success",
  "message": "Income statement CSV received and queued for processing",
  "filename": "income_statement_12_month-20260206.csv",
  "size_bytes": 123456,
  "batch_id": "20260206_143022",
  "timestamp": "2026-02-06T14:30:22.123Z"
}
```

### `POST /ingest-income-statement`
Direct CSV upload endpoint (for testing)

**Request:** Multipart form data with file

```bash
curl -X POST \
  https://your-railway-url.railway.app/ingest-income-statement \
  -F "file=@income_statement.csv"
```

**Response:**
```json
{
  "status": "success",
  "message": "Income statement CSV received and queued for processing",
  "filename": "income_statement.csv",
  "size_bytes": 123456,
  "batch_id": "20260206_143022"
}
```

## Data Flow

```
AppFolio Email → Mailgun → Railway Service → Lovable Edge Function → Supabase
                    ↓
              CSV Attachment
                    ↓
            1. Verify signature (10ms)
            2. Read raw bytes (50ms)
            3. Respond 200 OK (5ms)
            4. Parse CSV in background (5s)
            5. Send to Lovable (2s)
```

## Security

### Mailgun Signature Verification

Every webhook request is verified using HMAC-SHA256:

```python
hmac_digest = hmac.new(
    key=MAILGUN_WEBHOOK_SECRET.encode('utf-8'),
    msg=f"{timestamp}{token}".encode('utf-8'),
    digestmod=hashlib.sha256
).hexdigest()

is_valid = hmac.compare_digest(signature, hmac_digest)
```

### Bearer Token Authentication

All requests to Lovable use Bearer token:

```python
headers = {
    'Authorization': f'Bearer {INCOME_STATEMENT_WEBHOOK_TOKEN}',
    'Content-Type': 'application/json'
}
```

## Parsed Data Structure

### Metadata
```json
{
  "report_period": "2025",
  "period_start": "2025-01-01",
  "period_end": "2025-12-31",
  "upload_date": "2026-02-06T14:30:22.123Z",
  "total_categories": 209,
  "total_data_points": 2508,
  "month_columns": ["Jan 2025", "Feb 2025", ..., "Dec 2025"]
}
```

### Categories
```json
{
  "category_id": "cat_0",
  "account_name": "Total Operating Income",
  "category_level": 0,
  "category_type": "income",
  "parent_category": null,
  "is_total": true,
  "display_order": 0
}
```

### Monthly Data
```json
{
  "category_id": "cat_0",
  "account_name": "Total Operating Income",
  "month_year": "Jan 2025",
  "amount": 78172.29
}
```

### Totals
```json
{
  "total_operating_income": 938067.46,
  "total_cogs": 311004.64,
  "real_revenue": 627062.82,
  "total_operating_expense": 627062.82,
  "noi": 311004.64,
  "total_income": 939101.95,
  "total_expense": 628971.46,
  "net_income": 310130.49
}
```

## Development

### Local Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export LOVABLE_WEBHOOK_URL="https://your-lovable-url.supabase.co/functions/v1/ingest-income-statement"
export INCOME_STATEMENT_WEBHOOK_TOKEN="your-token"
export MAILGUN_WEBHOOK_SECRET="your-secret"

# Run service
python main.py
```

### Testing

```bash
# Test health check
curl http://localhost:8000/

# Test status
curl http://localhost:8000/status

# Test direct upload
curl -X POST \
  http://localhost:8000/ingest-income-statement \
  -F "file=@income_statement.csv"
```

## Monitoring

### Railway Logs

View logs in Railway dashboard → Logs tab

### Key Log Messages

- ✅ `Mailgun signature verified` - Security check passed
- 📎 `Found CSV attachment` - CSV file detected
- ✅ `Responding 200 OK to Mailgun immediately` - Fast response sent
- 🔄 `Starting income statement processing` - Background processing started
- ✅ `Parsed X categories, Y monthly data points` - CSV parsed successfully
- ✅ `Successfully sent income statement data to Lovable` - Data delivered

### Error Messages

- ❌ `Invalid Mailgun signature` - Wrong secret or tampered request
- ❌ `No income statement CSV found` - No CSV in email
- ❌ `Failed to send data to Lovable` - Webhook call failed
- ❌ `Error processing income statement` - Parsing or processing error

## Troubleshooting

### Mailgun Timeout

**Symptom:** Mailgun retries webhook multiple times

**Cause:** Service taking >5 seconds to respond

**Fix:** Ensure CSV parsing is in background task (already implemented)

### Invalid Signature

**Symptom:** `401 Unauthorized` response

**Cause:** Wrong `MAILGUN_WEBHOOK_SECRET` or whitespace

**Fix:** 
1. Copy secret from Mailgun dashboard
2. Ensure no leading/trailing whitespace
3. Service automatically strips whitespace with `.strip()`

### Failed to Send to Lovable

**Symptom:** `Failed to send data to Lovable` in logs

**Cause:** Wrong URL, token, or Lovable service down

**Fix:**
1. Verify `LOVABLE_WEBHOOK_URL` is correct
2. Verify `INCOME_STATEMENT_WEBHOOK_TOKEN` matches Lovable
3. Check Lovable edge function logs

## Documentation

- [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) - Complete deployment instructions
- [AI_SEARCH_FEATURE_DESIGN.md](../AI_SEARCH_FEATURE_DESIGN.md) - AI search implementation
- [INCOME_STATEMENT_2025_ANALYSIS.md](../INCOME_STATEMENT_2025_ANALYSIS.md) - Data analysis

## Version

**Version:** 2.0.0  
**Last Updated:** February 6, 2026  
**Based on:** Master Data Service learnings

## License

Proprietary - Abodex Hub
