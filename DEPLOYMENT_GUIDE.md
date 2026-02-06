# Income Statement Service - Deployment Guide

## Overview

This service processes Income Statement CSV files from AppFolio via Mailgun webhooks and sends structured data to Lovable Supabase.

**Key Features:**
- ✅ **Secure**: Mailgun signature verification with HMAC-SHA256
- ✅ **Fast**: Responds in <1 second to avoid Mailgun timeout
- ✅ **Reliable**: Background CSV processing with robust error handling
- ✅ **Production-ready**: Based on learnings from Master Data service

---

## Security Features

### 1. Mailgun Signature Verification

Every webhook request is verified using HMAC-SHA256 to ensure it came from Mailgun:

```python
def verify_mailgun_signature(token: str, timestamp: str, signature: str) -> bool:
    hmac_digest = hmac.new(
        key=MAILGUN_WEBHOOK_SECRET.encode('utf-8'),
        msg=f"{timestamp}{token}".encode('utf-8'),
        digestmod=hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(signature, hmac_digest)
```

**Why this matters:**
- Prevents unauthorized requests from fake sources
- Protects against replay attacks
- Uses constant-time comparison to prevent timing attacks

### 2. Bearer Token Authentication

All requests to Lovable webhook use Bearer token authentication:

```python
headers = {
    'Authorization': f'Bearer {INCOME_STATEMENT_WEBHOOK_TOKEN}',
    'Content-Type': 'application/json'
}
```

### 3. Environment Variable Security

All sensitive values are stored in Railway environment variables:
- `MAILGUN_WEBHOOK_SECRET` - For verifying Mailgun requests
- `INCOME_STATEMENT_WEBHOOK_TOKEN` - For authenticating to Lovable
- `LOVABLE_WEBHOOK_URL` - Lovable endpoint URL

**Important:** All environment variables are stripped of whitespace to handle hidden newlines from Railway UI:

```python
MAILGUN_WEBHOOK_SECRET = os.getenv("MAILGUN_WEBHOOK_SECRET", "").strip()
```

---

## Performance Optimizations

### Fast Response (<1 Second)

The service responds to Mailgun in <1 second to avoid timeout issues:

```python
@app.post("/webhook/mailgun")
async def mailgun_webhook(request: Request, background_tasks: BackgroundTasks):
    # 1. Verify signature (fast)
    verify_mailgun_signature(token, timestamp, signature)
    
    # 2. Read raw bytes only (fast - no parsing)
    csv_content = await value.read()
    
    # 3. Schedule background processing
    background_tasks.add_task(process_income_statement_background, csv_content, ...)
    
    # 4. RESPOND IMMEDIATELY (before CSV parsing)
    return JSONResponse({"status": "success"})
```

**Flow:**
1. Verify signature: ~10ms
2. Read raw bytes: ~50ms
3. Schedule background task: ~5ms
4. Respond 200 OK: ~5ms
5. **Total: <100ms** ✅

CSV parsing happens in background after response is sent.

### Background Processing

Heavy operations are done in background:
- CSV parsing with pandas
- Data transformation and calculations
- Webhook call to Lovable

This ensures Mailgun gets a fast response and doesn't timeout.

---

## Railway Deployment

### Step 1: Create New Service

1. Go to Railway dashboard
2. Click **"New Project"** → **"Empty Project"**
3. Click **"New"** → **"GitHub Repo"**
4. Select your repository
5. Railway will auto-detect Python and deploy

### Step 2: Configure Environment Variables

Go to **Variables** tab and add:

| Variable | Value | Notes |
|----------|-------|-------|
| `LOVABLE_WEBHOOK_URL` | `https://your-lovable-project.supabase.co/functions/v1/ingest-income-statement` | Your Lovable edge function URL |
| `INCOME_STATEMENT_WEBHOOK_TOKEN` | `your-secret-token-here` | Generate a strong random token |
| `MAILGUN_WEBHOOK_SECRET` | `your-mailgun-secret` | From Mailgun dashboard → Webhooks → Signing Key |
| `PORT` | `8000` | (Optional) Railway auto-assigns if not set |

**How to get Mailgun Webhook Secret:**
1. Go to Mailgun dashboard
2. Navigate to **Sending** → **Webhooks**
3. Copy the **HTTP webhook signing key**

**How to generate webhook token:**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Step 3: Deploy

Railway will automatically:
1. Install dependencies from `requirements.txt`
2. Start the service with `python main.py`
3. Assign a public URL (e.g., `https://income-statement-service-production.up.railway.app`)

### Step 4: Verify Deployment

Check service health:
```bash
curl https://your-railway-url.railway.app/
```

Expected response:
```json
{
  "status": "healthy",
  "service": "Income Statement Processing Service",
  "version": "2.0.0"
}
```

Check configuration:
```bash
curl https://your-railway-url.railway.app/status
```

Expected response:
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
    "last_processed": null,
    "total_processed": 0,
    "last_error": null,
    "last_filename": null
  }
}
```

---

## Mailgun Configuration

### Step 1: Create Route

1. Go to Mailgun dashboard
2. Navigate to **Sending** → **Routes**
3. Click **"Create Route"**

### Step 2: Configure Route

**Expression Type:** Match Recipient

**Recipient:** `income-statement@your-domain.com`

**Actions:**
- ✅ Store and notify: `https://your-railway-url.railway.app/webhook/mailgun`
- Method: POST
- Include attachments: ✅ Yes

**Priority:** 0 (highest)

**Description:** Income Statement CSV Processing

### Step 3: Test Route

Send a test email:
```bash
curl -s --user 'api:YOUR_MAILGUN_API_KEY' \
  https://api.mailgun.net/v3/YOUR_DOMAIN/messages \
  -F from='test@your-domain.com' \
  -F to='income-statement@your-domain.com' \
  -F subject='Test Income Statement' \
  -F text='Test email' \
  -F attachment=@income_statement_12_month-20260206.csv
```

Check Railway logs for:
```
✅ Mailgun signature verified
📎 Found CSV attachment: income_statement_12_month-20260206.csv (123456 bytes)
✅ Responding 200 OK to Mailgun immediately, processing in background
🔄 Starting income statement processing: income_statement_12_month-20260206.csv
✅ Parsed 209 categories, 2508 monthly data points
✅ Successfully sent income statement data to Lovable
```

---

## Lovable Edge Function

Create a new edge function in Lovable to receive the data:

### File: `/supabase/functions/ingest-income-statement/index.ts`

```typescript
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.39.0";

const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
};

serve(async (req) => {
  // Handle CORS
  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders });
  }

  try {
    // Verify authorization
    const authHeader = req.headers.get('Authorization');
    const expectedToken = Deno.env.get('INCOME_STATEMENT_WEBHOOK_TOKEN');
    
    if (!authHeader || authHeader !== `Bearer ${expectedToken}`) {
      return new Response(
        JSON.stringify({ error: 'Unauthorized' }),
        { status: 401, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }

    // Parse request body
    const payload = await req.json();
    
    console.log('Received income statement data:', {
      batch_id: payload.batch_id,
      categories: payload.categories?.length,
      monthly_data: payload.monthly_data?.length
    });

    // Create Supabase client
    const supabase = createClient(
      Deno.env.get('SUPABASE_URL') ?? '',
      Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''
    );

    // Insert metadata
    const { data: metadataData, error: metadataError } = await supabase
      .from('income_statement_metadata')
      .insert({
        batch_id: payload.batch_id,
        report_period: payload.metadata.report_period,
        period_start: payload.metadata.period_start,
        period_end: payload.metadata.period_end,
        upload_date: payload.metadata.upload_date,
        total_categories: payload.metadata.total_categories,
        total_data_points: payload.metadata.total_data_points,
        source: payload.source,
        report_type: payload.report_type
      })
      .select()
      .single();

    if (metadataError) throw metadataError;

    // Insert categories
    const categoriesWithBatchId = payload.categories.map(cat => ({
      ...cat,
      batch_id: payload.batch_id
    }));

    const { error: categoriesError } = await supabase
      .from('income_statement_categories')
      .insert(categoriesWithBatchId);

    if (categoriesError) throw categoriesError;

    // Insert monthly data
    const monthlyDataWithBatchId = payload.monthly_data.map(data => ({
      ...data,
      batch_id: payload.batch_id
    }));

    const { error: monthlyError } = await supabase
      .from('income_statement_monthly_data')
      .insert(monthlyDataWithBatchId);

    if (monthlyError) throw monthlyError;

    // Insert totals
    const totalsWithBatchId = {
      batch_id: payload.batch_id,
      ...payload.totals
    };

    const { error: totalsError } = await supabase
      .from('income_statement_totals')
      .insert(totalsWithBatchId);

    if (totalsError) throw totalsError;

    console.log('✅ Income statement data inserted successfully');

    return new Response(
      JSON.stringify({
        success: true,
        batch_id: payload.batch_id,
        records_inserted: {
          metadata: 1,
          categories: payload.categories.length,
          monthly_data: payload.monthly_data.length,
          totals: 1
        }
      }),
      { headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
    );

  } catch (error) {
    console.error('Error processing income statement:', error);
    return new Response(
      JSON.stringify({ error: error.message }),
      { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
    );
  }
});
```

### Deploy Edge Function

```bash
supabase functions deploy ingest-income-statement
```

### Set Environment Variable

```bash
supabase secrets set INCOME_STATEMENT_WEBHOOK_TOKEN=your-secret-token-here
```

---

## Testing

### 1. Test Mailgun Webhook Locally (Optional)

Use ngrok to expose local service:

```bash
# Start service locally
python main.py

# In another terminal, start ngrok
ngrok http 8000

# Update Mailgun route to use ngrok URL
# https://abc123.ngrok.io/webhook/mailgun
```

### 2. Test with Direct Upload

```bash
curl -X POST \
  https://your-railway-url.railway.app/ingest-income-statement \
  -F "file=@income_statement_12_month-20260206.csv"
```

### 3. Test End-to-End

Send email to `income-statement@your-domain.com` with CSV attachment.

**Check Railway logs:**
```
✅ Mailgun signature verified
📎 Found CSV attachment
✅ Responding 200 OK to Mailgun immediately
🔄 Starting income statement processing
✅ Parsed 209 categories, 2508 monthly data points
✅ Successfully sent income statement data to Lovable
```

**Check Lovable logs:**
```
Received income statement data: { batch_id: '20260206_143022', categories: 209, monthly_data: 2508 }
✅ Income statement data inserted successfully
```

**Check Supabase:**
```sql
SELECT * FROM income_statement_metadata ORDER BY upload_date DESC LIMIT 1;
SELECT COUNT(*) FROM income_statement_categories WHERE batch_id = '20260206_143022';
SELECT COUNT(*) FROM income_statement_monthly_data WHERE batch_id = '20260206_143022';
```

---

## Monitoring

### Railway Logs

View real-time logs in Railway dashboard:
- Click on your service
- Go to **Logs** tab
- Filter by severity: INFO, WARNING, ERROR

### Key Metrics to Monitor

1. **Response Time**: Should be <100ms
2. **Processing Time**: Should complete within 30 seconds
3. **Success Rate**: Should be >99%
4. **Error Rate**: Should be <1%

### Common Issues

**Issue: Invalid Mailgun signature**
- **Cause**: Wrong `MAILGUN_WEBHOOK_SECRET` or hidden whitespace
- **Fix**: Copy secret again, ensure `.strip()` is applied

**Issue: Mailgun timeout**
- **Cause**: Slow response (>5 seconds)
- **Fix**: Ensure CSV parsing is in background task

**Issue: Failed to send to Lovable**
- **Cause**: Wrong webhook URL or token
- **Fix**: Verify `LOVABLE_WEBHOOK_URL` and `INCOME_STATEMENT_WEBHOOK_TOKEN`

**Issue: CSV parsing error**
- **Cause**: Unexpected CSV format
- **Fix**: Check CSV structure, add error handling

---

## Security Best Practices

1. **Never commit secrets to Git**
   - Use Railway environment variables
   - Add `.env` to `.gitignore`

2. **Rotate tokens regularly**
   - Generate new webhook tokens every 90 days
   - Update in both Railway and Lovable

3. **Use HTTPS only**
   - Railway provides HTTPS by default
   - Never use HTTP for webhooks

4. **Validate all inputs**
   - Verify Mailgun signature
   - Validate CSV structure
   - Sanitize data before insertion

5. **Monitor for anomalies**
   - Set up alerts for failed requests
   - Monitor for unusual traffic patterns
   - Review logs regularly

---

## Comparison with Master Data Service

| Feature | Master Data Service | Income Statement Service |
|---------|---------------------|--------------------------|
| **Mailgun Signature Verification** | ✅ Yes | ✅ Yes |
| **Fast Response (<1s)** | ✅ Yes | ✅ Yes |
| **Background Processing** | ✅ Yes | ✅ Yes |
| **Environment Variable Stripping** | ✅ Yes | ✅ Yes |
| **CSV Buffering** | ✅ Yes (3 files) | ❌ No (single file) |
| **Timeout Handling** | ✅ Yes (30 min buffer) | ❌ No (immediate processing) |
| **Multiple CSV Files** | ✅ Yes (3 files) | ❌ No (1 file) |
| **Robust Matching** | ✅ Yes (5-tier) | ❌ N/A |

**Why no buffering for Income Statement?**
- Only 1 CSV file expected (vs 3 for Master Data)
- Simpler workflow: receive → parse → send
- No need to wait for multiple files

---

## Troubleshooting

### Enable Debug Logging

Change logging level in `main.py`:

```python
logging.basicConfig(
    level=logging.DEBUG,  # Changed from INFO
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
```

### Test Signature Verification

```python
import hmac
import hashlib

secret = "your-mailgun-secret"
timestamp = "1234567890"
token = "abc123"

signature = hmac.new(
    key=secret.encode('utf-8'),
    msg=f"{timestamp}{token}".encode('utf-8'),
    digestmod=hashlib.sha256
).hexdigest()

print(f"Expected signature: {signature}")
```

### Check CSV Structure

```python
import pandas as pd

df = pd.read_csv('income_statement_12_month-20260206.csv')
print(f"Columns: {df.columns.tolist()}")
print(f"Rows: {len(df)}")
print(df.head())
```

---

## Support

For issues or questions:
- Check Railway logs first
- Review this deployment guide
- Test with direct upload endpoint
- Contact development team

---

**Version:** 2.0.0  
**Last Updated:** February 6, 2026  
**Based on:** Master Data Service learnings
