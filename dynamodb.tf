# One table, a partition per user: pk = USER#<email>. The current card of a
# contact is sk = CONTACT#<id>, every create, save, delete, restore and import
# of it is appended as sk = REV#<id>#<time>#<rand> with the whole card, and a
# deleted contact leaves sk = DELETED#<id> so that it can be found and
# restored. The page reads a user's cards with one query of the partition.
resource "aws_dynamodb_table" "main" {
  #checkov:skip=CKV_AWS_119:An AWS owned key avoids a dedicated KMS charge.
  #checkov:skip=CKV_AWS_28:Point-in-time recovery is on outside the demo, which is rebuilt every hour.
  name         = local.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  # The sync log and the sign-in states of Google go when "expires" (epoch
  # seconds) has passed.
  ttl {
    attribute_name = "expires"
    enabled        = true
  }

  # The demo is rebuilt every hour; a backup of it has nothing to bring back.
  point_in_time_recovery {
    enabled = !var.demo
  }

  server_side_encryption {
    enabled = true
  }
}
