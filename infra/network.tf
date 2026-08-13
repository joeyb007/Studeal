# Network: one VPC, two PUBLIC subnets (deliberate no-NAT posture — tasks get
# public IPs for egress to Bedrock/Browserbase/Resend, and security groups
# are the locked door: nothing accepts inbound except the ALB, and the ALB
# only forwards to tasks). Two subnets because ALBs require two AZs — AWS
# forcing basic fault tolerance.

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "studeal" }
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.${count.index + 1}.0/24"
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "studeal-public-${count.index + 1}" }
}

# The VPC's door to the internet; the route table sends non-local traffic
# through it. Without these, "public subnet" is just a name.
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "studeal" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "studeal-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------------------
# Security groups — the actual perimeter. The pattern to internalize: rules
# reference OTHER GROUPS, not IPs. "rds accepts 5432 from the tasks group"
# stays true no matter how tasks scale or which IPs they get.
# ---------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name_prefix = "studeal-alb-"
  vpc_id      = aws_vpc.main.id
  description = "Internet-facing load balancer"

  ingress {
    description = "HTTPS from anywhere"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP (redirected to HTTPS by the listener)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "tasks" {
  name_prefix = "studeal-tasks-"
  vpc_id      = aws_vpc.main.id
  description = "ECS tasks (api/worker/beat)"

  ingress {
    description     = "API traffic, ONLY from the ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # Full egress: Bedrock, Browserbase CDP, Resend, Stripe, Google.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "rds" {
  name_prefix = "studeal-rds-"
  vpc_id      = aws_vpc.main.id
  description = "Postgres, reachable only from tasks"

  ingress {
    description     = "Postgres from ECS tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "redis" {
  name_prefix = "studeal-redis-"
  vpc_id      = aws_vpc.main.id
  description = "Redis, reachable only from tasks"

  ingress {
    description     = "Redis from ECS tasks"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }

  lifecycle {
    create_before_destroy = true
  }
}
