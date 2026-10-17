data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "ReadPhotosSigningKey"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.photos_signing_key.arn]
  }

  statement {
    sid = "Logs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [for group in concat(
      [aws_cloudwatch_log_group.lambda],
      aws_cloudwatch_log_group.dav,
      aws_cloudwatch_log_group.google,
      aws_cloudwatch_log_group.icloud,
    ) : "${group.arn}:*"]
  }

  statement {
    sid = "ContactsTable"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:TransactWriteItems",
    ]
    resources = [aws_dynamodb_table.main.arn]
  }

  # Photos are uploaded by the page through a presigned POST, or put here
  # from an imported vCard; exports are written here and read through a
  # presigned link.
  statement {
    sid = "PhotosAndExports"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject",
    ]
    resources = [
      "${aws_s3_bucket.photos.arn}/photos/*",
      "${aws_s3_bucket.photos.arn}/exports/*",
    ]
  }

  statement {
    sid       = "ListPhotos"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.photos.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["photos/*"]
    }
  }

  statement {
    sid       = "GoogleClientSecret"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.google_secret_parameter}"]
  }

  statement {
    sid = "GoogleTokens"
    actions = [
      "ssm:GetParameter",
      "ssm:PutParameter",
      "ssm:DeleteParameter",
    ]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.google_token_prefix}/*"]
  }

  # "Sync now" and the sync after connecting start the sync function.
  statement {
    sid       = "StartGoogleSync"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${var.region}:${local.account_id}:function:${local.google_function}"]
  }

  statement {
    sid = "ICloudPasswords"
    actions = [
      "ssm:GetParameter",
      "ssm:PutParameter",
      "ssm:DeleteParameter",
    ]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.icloud_password_prefix}/*"]
  }

  # "Sync now" and the sync after connecting start the iCloud sync function.
  statement {
    sid       = "StartICloudSync"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${var.region}:${local.account_id}:function:${local.icloud_function}"]
  }

  statement {
    sid = "ManageUsers"
    actions = [
      "cognito-idp:ListUsers",
      "cognito-idp:ListUsersInGroup",
      "cognito-idp:AdminGetUser",
      "cognito-idp:AdminSetUserMFAPreference",
      "cognito-idp:AdminCreateUser",
      "cognito-idp:AdminDeleteUser",
      "cognito-idp:AdminDisableUser",
      "cognito-idp:AdminEnableUser",
      "cognito-idp:AdminSetUserPassword",
      "cognito-idp:AdminAddUserToGroup",
      "cognito-idp:AdminRemoveUserFromGroup",
    ]
    resources = [aws_cognito_user_pool.main.arn]
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name}-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${local.name}-api"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}
