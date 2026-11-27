# The full export (lambda/backup.py): Settings → Export starts this function
# in the background, which writes a ZIP archive of the whole address book
# (cards, history, trash, groups, the sync log and the photos) to exports/ of
# the photos bucket. A book with many photos takes longer than API Gateway
# waits, so the API only starts it and the page asks for the result.

resource "aws_cloudwatch_log_group" "backup" {
  #checkov:skip=CKV_AWS_158:CloudWatch's own encryption is enough for these logs. A customer managed key would add a monthly charge.
  #checkov:skip=CKV_AWS_338:Thirty days is enough for this private app and keeps the log bill small.
  name              = "/aws/lambda/${local.backup_function}"
  retention_in_days = 30
}

resource "aws_lambda_function" "backup" {
  #checkov:skip=CKV_AWS_50:X-Ray is disabled to avoid extra cost on a low-traffic app.
  #checkov:skip=CKV_AWS_115:Reserved concurrency is left to the account pool.
  #checkov:skip=CKV_AWS_116:A failed export is shown on the page, which starts another one.
  #checkov:skip=CKV_AWS_117:The function only calls public AWS endpoints for DynamoDB and S3.
  #checkov:skip=CKV_AWS_173:Environment values are resource names, not secrets.
  #checkov:skip=CKV_AWS_272:Code signing is not used for this internal function.
  function_name = local.backup_function
  role          = aws_iam_role.lambda.arn
  runtime       = "python3.14"
  architectures = ["arm64"]
  handler       = "backup.handler"
  memory_size   = 1024
  # Every photo is read from S3 one after another.
  timeout = 900
  # The archive is written to /tmp before it goes to S3.
  ephemeral_storage {
    size = 4096
  }

  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  environment {
    variables = {
      TABLE_NAME    = aws_dynamodb_table.main.name
      PHOTOS_BUCKET = aws_s3_bucket.photos.id
      UID_DOMAIN    = local.site_host
      DEMO          = tostring(var.demo)
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.backup.name
  }

  depends_on = [aws_iam_role_policy.lambda]
}

# A failed export is not retried: the page shows the error.
resource "aws_lambda_function_event_invoke_config" "backup" {
  function_name          = aws_lambda_function.backup.function_name
  maximum_retry_attempts = 0
}
