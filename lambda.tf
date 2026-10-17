resource "aws_cloudwatch_log_group" "lambda" {
  #checkov:skip=CKV_AWS_158:CloudWatch's own encryption is enough for these logs. A customer managed key would add a monthly charge.
  #checkov:skip=CKV_AWS_338:Thirty days is enough for this private app and keeps the log bill small.
  name              = "/aws/lambda/${local.name}-api"
  retention_in_days = 30
}

# The code without the tests. In the stack root, as in the other stacks: CI
# carries the files a plan makes to the apply job, but nothing under
# .terraform/. Git ignores it.
data "archive_file" "api" {
  type = "zip"
  source {
    content  = file("${path.module}/lambda/contacts.py")
    filename = "contacts.py"
  }
  source {
    content  = file("${path.module}/lambda/vcard.py")
    filename = "vcard.py"
  }
  source {
    content  = file("${path.module}/lambda/carddav.py")
    filename = "carddav.py"
  }
  source {
    content  = file("${path.module}/lambda/google_sync.py")
    filename = "google_sync.py"
  }
  source {
    content  = file("${path.module}/lambda/icloud_sync.py")
    filename = "icloud_sync.py"
  }
  output_path      = "${path.module}/.build/api.zip"
  output_file_mode = "0644"
}

resource "aws_lambda_function" "api" {
  #checkov:skip=CKV_AWS_50:X-Ray is disabled to avoid extra cost on a low-traffic app.
  #checkov:skip=CKV_AWS_115:Reserved concurrency is left to the account pool.
  #checkov:skip=CKV_AWS_116:This function is synchronous. Failures are returned to the caller.
  #checkov:skip=CKV_AWS_117:The function only calls public AWS endpoints for DynamoDB, S3, SSM and Cognito, and the release feed on GitHub.
  #checkov:skip=CKV_AWS_173:Environment values are resource names, not secrets.
  #checkov:skip=CKV_AWS_272:Code signing is not used for this internal function.
  function_name = "${local.name}-api"
  role          = aws_iam_role.lambda.arn
  runtime       = "python3.14"
  architectures = ["arm64"]
  handler       = "contacts.handler"
  memory_size   = 512
  # API Gateway waits 30 seconds; an import of a batch of vCards with photos
  # takes the longest.
  timeout = 29

  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  environment {
    variables = {
      VERSION                   = local.version
      RELEASES_REPOSITORY       = var.releases_repository
      TABLE_NAME                = aws_dynamodb_table.main.name
      PHOTOS_BUCKET             = aws_s3_bucket.photos.id
      PHOTO_MAX_BYTES           = tostring(var.photo_max_bytes)
      CLOUDFRONT_KEY_PAIR_ID    = aws_cloudfront_public_key.photos.id
      PHOTOS_KEY_PARAMETER_NAME = aws_ssm_parameter.photos_signing_key.name
      USER_POOL_ID              = aws_cognito_user_pool.main.id
      ADMIN_GROUP               = aws_cognito_user_group.admins.name
      DAV_URL                   = local.dav ? "https://${var.dav_domain_name}" : ""
      SITE_URL                  = local.site_url
      UID_DOMAIN                = local.site_host
      GOOGLE_CLIENT_ID          = var.google_client_id
      GOOGLE_SECRET_PARAMETER   = local.google_secret_parameter
      GOOGLE_TOKEN_PREFIX       = local.google_token_prefix
      GOOGLE_SYNC_FUNCTION      = local.google ? local.google_function : ""
      ICLOUD_PASSWORD_PREFIX    = local.icloud ? local.icloud_password_prefix : ""
      ICLOUD_SYNC_FUNCTION      = local.icloud ? local.icloud_function : ""
      # The demo shows the sync with Google, iCloud and devices without
      # running it, and keeps the accounts as they are.
      DEMO = tostring(var.demo)
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda.name
  }

  depends_on = [aws_iam_role_policy.lambda]
}
