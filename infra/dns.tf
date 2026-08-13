# Route53 hosted zone for studeal.site. The domain stays REGISTERED at
# Namecheap; this zone becomes its AUTHORITY once Namecheap's nameservers
# are pointed here (registrar = where you pay, zone = who answers queries).

resource "aws_route53_zone" "main" {
  name = "studeal.site"
}

output "nameservers" {
  description = "Paste these 4 into Namecheap: Domain → Nameservers → Custom DNS"
  value       = aws_route53_zone.main.name_servers
}
