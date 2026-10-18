# Sync with Google Contacts (lambda/google_sync.py): a function that the
# schedule wakes every google_sync_minutes for the address books connected to
# Google, and that "Sync now" starts. Google comes back from its consent page
# to /api/oauth/google, a route without the Cognito authorizer: the state it
# carries is checked instead. With google_client_id empty the schedule is off
# and the page offers no Google.

resource "aws_cloudwatch_log_group" "google" {
  #checkov:skip=CKV_AWS_158:CloudWatch's own encryption is enough for these logs. A customer managed key would add a monthly charge.
  #checkov:skip=CKV_AWS_338:Thirty days is enough for this private app and keeps the log bill small.
  count = local.google ? 1 : 0

  name              = "/aws/lambda/${local.google_function}"
  retention_in_days = 30
}

resource "aws_lambda_function" "google" {
  #checkov:skip=CKV_AWS_50:X-Ray is disabled to avoid extra cost on a low-traffic app.
  #checkov:skip=CKV_AWS_115:Reserved concurrency is left to the account pool.
  #checkov:skip=CKV_AWS_116:A failed sync is written to the sync log and mailed to its user; the next one comes on schedule.
  #checkov:skip=CKV_AWS_117:The function calls the People API of Google and public AWS endpoints.
  #checkov:skip=CKV_AWS_173:Environment values are names and the public OAuth client id, not secrets.
  #checkov:skip=CKV_AWS_272:Code signing is not used for this internal function.
  count = local.google ? 1 : 0

  function_name = local.google_function
  role          = aws_iam_role.lambda.arn
  runtime       = "python3.14"
  architectures = ["arm64"]
  handler       = "google_sync.handler"
  memory_size   = 512
  # A first sync of a big address book writes in batches of 200.
  timeout = 300

  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  environment {
    variables = {
      TABLE_NAME              = aws_dynamodb_table.main.name
      PHOTOS_BUCKET           = aws_s3_bucket.photos.id
      PHOTO_MAX_BYTES         = tostring(var.photo_max_bytes)
      USER_POOL_ID            = aws_cognito_user_pool.main.id
      SITE_URL                = local.site_url
      UID_DOMAIN              = local.site_host
      GOOGLE_CLIENT_ID        = var.google_client_id
      GOOGLE_SECRET_PARAMETER = local.google_secret_parameter
      GOOGLE_TOKEN_PREFIX     = local.google_token_prefix
      GOOGLE_SYNC_FUNCTION    = local.google_function
      SENDER                  = local.alerts ? var.sender_address : ""
      SYNC_ALERT_FAILURES     = tostring(var.sync_alert_failures)
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.google[0].name
  }

  depends_on = [aws_iam_role_policy.lambda]

  lifecycle {
    precondition {
      condition     = !local.alerts || (local.custom_domain && endswith(var.sender_address, "@${var.domain_name}"))
      error_message = "sender_address has to be on domain_name, which the stack makes an SES identity."
    }
  }
}

# A sync is not retried: the next one comes on schedule.
resource "aws_lambda_function_event_invoke_config" "google" {
  count = local.google ? 1 : 0

  function_name          = aws_lambda_function.google[0].function_name
  maximum_retry_attempts = 0
}

resource "aws_cloudwatch_event_rule" "google" {
  count = local.google ? 1 : 0

  name                = "${local.name}-google"
  description         = "Syncs the address books connected to Google Contacts."
  schedule_expression = "rate(${var.google_sync_minutes} minutes)"
}

resource "aws_cloudwatch_event_target" "google" {
  count = local.google ? 1 : 0

  rule  = aws_cloudwatch_event_rule.google[0].name
  arn   = aws_lambda_function.google[0].arn
  input = jsonencode({ action = "tick" })
}

resource "aws_lambda_permission" "google" {
  count = local.google ? 1 : 0

  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.google[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.google[0].arn
}

# Google's redirect after the consent page; the API checks the state.
resource "aws_apigatewayv2_route" "google_callback" {
  #checkov:skip=CKV_AWS_309:Google comes back without a Cognito token; the one-time state of the sign-in is checked instead.
  count = local.google ? 1 : 0

  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /api/oauth/google"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}
