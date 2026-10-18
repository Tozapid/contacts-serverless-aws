# Sync with iCloud Contacts (lambda/icloud_sync.py): a function that the
# schedule wakes every icloud_sync_minutes for the address books connected to
# iCloud, and that "Sync now" starts. It is a CardDAV client of
# contacts.icloud.com with each user's Apple ID and app-specific password,
# which the page puts into SSM under icloud_password_prefix.

resource "aws_cloudwatch_log_group" "icloud" {
  #checkov:skip=CKV_AWS_158:CloudWatch's own encryption is enough for these logs. A customer managed key would add a monthly charge.
  #checkov:skip=CKV_AWS_338:Thirty days is enough for this private app and keeps the log bill small.
  count = local.icloud ? 1 : 0

  name              = "/aws/lambda/${local.icloud_function}"
  retention_in_days = 30
}

resource "aws_lambda_function" "icloud" {
  #checkov:skip=CKV_AWS_50:X-Ray is disabled to avoid extra cost on a low-traffic app.
  #checkov:skip=CKV_AWS_115:Reserved concurrency is left to the account pool.
  #checkov:skip=CKV_AWS_116:A failed sync is written to the sync log and mailed to its user; the next one comes on schedule.
  #checkov:skip=CKV_AWS_117:The function calls the CardDAV server of iCloud and public AWS endpoints.
  #checkov:skip=CKV_AWS_173:Environment values are names, not secrets; the passwords are in SSM.
  #checkov:skip=CKV_AWS_272:Code signing is not used for this internal function.
  count = local.icloud ? 1 : 0

  function_name = local.icloud_function
  role          = aws_iam_role.lambda.arn
  runtime       = "python3.14"
  architectures = ["arm64"]
  handler       = "icloud_sync.handler"
  memory_size   = 512
  # A first sync of a big address book writes up to 300 cards one by one.
  timeout = 300

  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  environment {
    variables = {
      TABLE_NAME             = aws_dynamodb_table.main.name
      PHOTOS_BUCKET          = aws_s3_bucket.photos.id
      PHOTO_MAX_BYTES        = tostring(var.photo_max_bytes)
      USER_POOL_ID           = aws_cognito_user_pool.main.id
      SITE_URL               = local.site_url
      UID_DOMAIN             = local.site_host
      ICLOUD_PASSWORD_PREFIX = local.icloud_password_prefix
      ICLOUD_SYNC_FUNCTION   = local.icloud_function
      SENDER                 = local.alerts ? var.sender_address : ""
      SYNC_ALERT_FAILURES    = tostring(var.sync_alert_failures)
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.icloud[0].name
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
resource "aws_lambda_function_event_invoke_config" "icloud" {
  count = local.icloud ? 1 : 0

  function_name          = aws_lambda_function.icloud[0].function_name
  maximum_retry_attempts = 0
}

resource "aws_cloudwatch_event_rule" "icloud" {
  count = local.icloud ? 1 : 0

  name                = "${local.name}-icloud"
  description         = "Syncs the address books connected to iCloud Contacts."
  schedule_expression = "rate(${var.icloud_sync_minutes} minutes)"
}

resource "aws_cloudwatch_event_target" "icloud" {
  count = local.icloud ? 1 : 0

  rule  = aws_cloudwatch_event_rule.icloud[0].name
  arn   = aws_lambda_function.icloud[0].arn
  input = jsonencode({ action = "tick" })
}

resource "aws_lambda_permission" "icloud" {
  count = local.icloud ? 1 : 0

  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.icloud[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.icloud[0].arn
}
