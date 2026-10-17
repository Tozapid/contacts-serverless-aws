# Production: your own domains, the sync with devices, Google and iCloud, no
# public sign-in and no hourly reset. Copy to terraform.tfvars and fill in.

name = "contacts"
demo = false

domain_name       = "contacts.example.com"
dav_domain_name   = "dav.contacts.example.com"
route53_zone_name = "example.com"

admin_email    = "you@example.com"
admin_password = "" # Cognito mails a temporary password instead

reset_schedule = ""

# A web application client of a Google Cloud project with the People API; its
# secret goes into SSM by hand (see README). "" leaves Google out.
google_client_id = ""
icloud_enabled   = true
