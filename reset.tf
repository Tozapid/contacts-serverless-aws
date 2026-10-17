# Public demo: every hour everything goes back to a fresh deployment. Set
# reset_schedule to "" to keep data (and none of this is made).

resource "aws_ssm_parameter" "admin_password" {
  #checkov:skip=CKV_AWS_337:The AWS managed SSM key is enough for a demo password that the README publishes.
  count = local.reset ? 1 : 0

  name        = "/${local.name}/admin-password"
  description = "Password the hourly reset gives back to the demo user."
  type        = "SecureString"
  value       = var.admin_password

  lifecycle {
    precondition {
      condition     = var.admin_password != ""
      error_message = "The reset gives the demo user admin_password back: set one, or turn the reset off with reset_schedule = \"\"."
    }
  }
}

# The reset writes the examples through the code of the API.
data "archive_file" "reset" {
  type             = "zip"
  output_path      = "${path.module}/.build/reset.zip"
  output_file_mode = "0644"

  source {
    content  = file("${path.module}/lambda/reset.py")
    filename = "reset.py"
  }

  source {
    content  = file("${path.module}/lambda/contacts.py")
    filename = "contacts.py"
  }

  source {
    content  = file("${path.module}/lambda/vcard.py")
    filename = "vcard.py"
  }

  source {
    content  = file("${path.module}/seed/seed.json")
    filename = "seed.json"
  }
}

resource "aws_cloudwatch_log_group" "reset" {
  #checkov:skip=CKV_AWS_158:CloudWatch's own encryption is enough for these logs. A customer managed key would add a monthly charge.
  #checkov:skip=CKV_AWS_338:Thirty days is enough for reset logs.
  count = local.reset ? 1 : 0

  name              = "/aws/lambda/${local.name}-reset"
  retention_in_days = 30
}

data "aws_iam_policy_document" "reset" {
  count = local.reset ? 1 : 0

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.reset[0].arn}:*"]
  }

  statement {
    sid = "Table"
    actions = [
      "dynamodb:Scan",
      "dynamodb:Query",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:TransactWriteItems",
    ]
    resources = [aws_dynamodb_table.main.arn]
  }

  # The photos and exports of the visitors go with the address books.
  statement {
    sid       = "ListPhotos"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.photos.arn]
  }

  statement {
    sid       = "DeletePhotos"
    actions   = ["s3:DeleteObject"]
    resources = ["${aws_s3_bucket.photos.arn}/*"]
  }

  statement {
    sid = "Users"
    actions = [
      "cognito-idp:ListUsers",
      "cognito-idp:AdminCreateUser",
      "cognito-idp:AdminDeleteUser",
      "cognito-idp:AdminEnableUser",
      "cognito-idp:AdminSetUserPassword",
      "cognito-idp:AdminSetUserMFAPreference",
      "cognito-idp:AdminAddUserToGroup",
      "cognito-idp:AdminDeleteUserAttributes",
      "cognito-idp:AdminUserGlobalSignOut",
    ]
    resources = [aws_cognito_user_pool.main.arn]
  }

  statement {
    sid       = "DemoPassword"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.admin_password[0].arn]
  }
}

resource "aws_iam_role" "reset" {
  count = local.reset ? 1 : 0

  name               = "${local.name}-reset"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "reset" {
  count = local.reset ? 1 : 0

  name   = "${local.name}-reset"
  role   = aws_iam_role.reset[0].id
  policy = data.aws_iam_policy_document.reset[0].json
}

resource "aws_lambda_function" "reset" {
  #checkov:skip=CKV_AWS_50:X-Ray is off to keep the demo free.
  #checkov:skip=CKV_AWS_115:Reserved concurrency is left to the account pool.
  #checkov:skip=CKV_AWS_116:A failed reset simply runs again next hour.
  #checkov:skip=CKV_AWS_117:The function only calls public AWS endpoints.
  #checkov:skip=CKV_AWS_173:Environment values are resource names, not secrets.
  #checkov:skip=CKV_AWS_272:Code signing is not used here.
  count = local.reset ? 1 : 0

  function_name = "${local.name}-reset"
  role          = aws_iam_role.reset[0].arn
  runtime       = "python3.14"
  architectures = ["arm64"]
  handler       = "reset.handler"
  memory_size   = 256
  timeout       = 120

  filename         = data.archive_file.reset.output_path
  source_code_hash = data.archive_file.reset.output_base64sha256

  environment {
    variables = {
      TABLE_NAME              = aws_dynamodb_table.main.name
      PHOTOS_BUCKET           = aws_s3_bucket.photos.id
      USER_POOL_ID            = aws_cognito_user_pool.main.id
      ADMIN_GROUP             = aws_cognito_user_group.admins.name
      UID_DOMAIN              = local.site_host
      DEMO_EMAIL              = var.admin_email
      DEMO_PASSWORD_PARAMETER = aws_ssm_parameter.admin_password[0].name
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.reset[0].name
  }

  depends_on = [aws_iam_role_policy.reset]
}

resource "aws_cloudwatch_event_rule" "reset" {
  count = local.reset ? 1 : 0

  name                = "${local.name}-reset"
  description         = "Put the address book demo back to its initial state."
  schedule_expression = var.reset_schedule
}

resource "aws_cloudwatch_event_target" "reset" {
  count = local.reset ? 1 : 0

  rule = aws_cloudwatch_event_rule.reset[0].name
  arn  = aws_lambda_function.reset[0].arn
}

resource "aws_lambda_permission" "reset" {
  count = local.reset ? 1 : 0

  statement_id  = "AllowScheduledReset"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reset[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reset[0].arn
}

# The first examples right away, not only after the first hour.
resource "aws_lambda_invocation" "first_reset" {
  count = local.reset ? 1 : 0

  function_name = aws_lambda_function.reset[0].function_name
  input         = jsonencode({ reason = "deploy" })

  triggers = {
    code = data.archive_file.reset.output_base64sha256
  }

  depends_on = [aws_cognito_user_in_group.admin]
}
