variable "region" {
  type        = string
  default     = "eu-central-1"
  description = "Region of every resource but CloudFront: the API, the data, the photos and the sign-in."
}

variable "name" {
  type        = string
  default     = "contacts-serverless"
  description = "Prefix of every resource name; bucket names add the account id. The deploy role of the demo is limited to it."
}

variable "demo" {
  type        = bool
  default     = true
  description = "Public demo: the sign-in screen shows and fills in the demo sign-in, the settings hide the password and two-factor authentication, administrators may look at the users but not change them, and the sync with Google, iCloud and devices is shown but not run."
}

variable "domain_name" {
  type        = string
  default     = ""
  description = "Name of the page, such as contacts.example.com, in the Route 53 zone route53_zone_name. Empty: the page answers at its CloudFront address (the demo)."
}

variable "route53_zone_name" {
  type        = string
  default     = ""
  description = "Public Route 53 zone of domain_name and dav_domain_name; the stack writes the certificates' validation and the alias records there."
}

variable "dav_domain_name" {
  type        = string
  default     = ""
  description = "Name of the CardDAV server that Apple Contacts, Thunderbird and other clients sync with, such as dav.contacts.example.com: an API Gateway domain in the stack region, as CloudFront does not pass PROPFIND and REPORT. Empty: no CardDAV server (the demo)."
}

variable "admin_email" {
  type        = string
  default     = "demo@example.com"
  description = "Sign-in of the first user, an administrator. Self sign-up is off; administrators invite the others on the page."
}

variable "admin_password" {
  type        = string
  default     = "Demo-Password-1"
  sensitive   = true
  description = "Permanent password of the first user, published in the demo and put back by the reset. Empty: Cognito mails admin_email a temporary password instead."
}

variable "reset_schedule" {
  type        = string
  default     = "rate(1 hour)"
  description = "When the demo goes back to its initial state: the address books are wiped and the examples of seed/seed.json written again, the first user gets admin_password back and other users go. An empty string turns the reset off."
}

variable "photo_max_bytes" {
  type        = number
  default     = 2097152
  description = "Maximum size of one contact photo. The page shrinks a photo to 1024 pixels before the upload."
}

variable "google_client_id" {
  type        = string
  default     = ""
  description = "OAuth client id of a Google Cloud project with the People API for the sync with Google Contacts (a web application client). Its secret goes by hand into the SSM parameter /<name>/google-client-secret. Empty: no sync with Google (the demo shows it without running it)."
}

variable "google_sync_minutes" {
  type        = number
  default     = 5
  description = "How often the address books connected to Google sync with it, in minutes."
}

variable "icloud_enabled" {
  type        = bool
  default     = false
  description = "Runs the sync with iCloud Contacts (Apple ID and an app-specific password, lambda/icloud_sync.py). False: no sync with iCloud (the demo shows it without running it)."
}

variable "icloud_sync_minutes" {
  type        = number
  default     = 5
  description = "How often the address books connected to iCloud are synced, in minutes."
}

variable "sender_address" {
  type        = string
  default     = ""
  description = "From address of the mails that tell a user their sync with Google or iCloud fails, such as alert@contacts.example.com, on domain_name, which the stack makes an SES identity (ses.tf). Empty, or the demo: no SES identity and no mails."

  validation {
    condition     = var.sender_address == "" || can(regex("^[^@\\s]+@[^@\\s]+$", var.sender_address))
    error_message = "sender_address is an email address or empty."
  }
}

variable "sync_alert_failures" {
  type        = number
  default     = 3
  description = "Failed syncs with Google or iCloud in a row after which the user gets a mail. A lost sign-in, which no later sync mends, sends it at once."

  validation {
    condition     = var.sync_alert_failures >= 1
    error_message = "sync_alert_failures is at least 1."
  }
}

variable "releases_repository" {
  type        = string
  default     = "Tozapid/contacts-serverless-aws"
  description = "GitHub repository (owner/name) whose releases the settings compare the version of the site with. Empty turns the check off."
}

variable "tags" {
  type = map(string)
  default = {
    Managed = "IAC"
    Project = "contacts-serverless-aws"
  }
  description = "Tags applied to supported resources."
}
