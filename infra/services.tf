# The three long-running services — one image, three commands, exactly the
# docker-compose trio translated to Fargate. Config philosophy: secrets come
# from SSM at container start; plain tuning numbers live here in code where
# a change is a reviewable diff.

locals {
  image = "${aws_ecr_repository.app.repository_url}:v1"

  common_env = [
    { name = "LLM_BACKEND", value = "bedrock" },
    { name = "EMBEDDING_BACKEND", value = "bedrock-mm" },
    { name = "AWS_REGION", value = "us-east-1" },
    { name = "APP_BASE_URL", value = "https://studeal.site" },
    { name = "RESEND_FROM", value = "Studeal <alerts@studeal.site>" },
    { name = "STRIPE_SUCCESS_URL", value = "https://studeal.site/dashboard?upgraded=1" },
    { name = "STRIPE_CANCEL_URL", value = "https://studeal.site/watchlists" },
    # Backend split (probe 2026-08-14): AgentCore (credits) is the default;
    # fingerprint-sensitive sites pin browserbase in MarketplaceConfig.
    # Media blocking keeps the residential-proxy GB inside the plan's 1 GB.
    { name = "AGENT_BROWSER_BACKEND", value = "agentcore" },
    # Capacity model (2026-08-11): 6 lanes x 4 hunts = 24 sessions; the
    # browserbase cap now only governs the pinned lanes (25-session plan).
    { name = "AGENT_LANE_CONCURRENCY", value = "6" },
    { name = "AGENT_NAV_CONCURRENCY", value = "6" },
    { name = "BROWSERBASE_MAX_SESSIONS", value = "24" },
    { name = "AGENTCORE_MAX_SESSIONS", value = "24" },
    { name = "FLEET_MAX_CONCURRENT_HUNTS", value = "4" },
    { name = "AGENT_LANE_DEADLINE_S", value = "420" },
    { name = "HUNT_BROWSE_DEADLINE_S", value = "1200" },
    # Spend guards + freshness + email throttle. LLM is ~96% of credit burn
    # (2026-08-16 Cost Explorer: Haiku $198 + Sonnet $93 of $303 total; all
    # infra ~$23/mo, AgentCore browsers $1.72). Credits expire 2028-08-31, so
    # $416/mo is the break-even pace and the studeal-gross-burn budget alerts
    # against it. Raised for launch (headroom for demo + first traffic); drop
    # to 25 once traffic settles, which lands typical usage under the pace.
    { name = "DAILY_LLM_BUDGET_USD", value = "150" },
    { name = "DAILY_BROWSER_SESSION_CAP", value = "300" },
    # Residential proxy for FB lanes (agentcore + prepaid DataImpulse). The
    # secret holds {username,password}; AgentCore reads it via the ARN so the
    # password never lands in env. Hard caps below bound the ONLY metered
    # dollar left: 600/month ≈ 15 GB ≈ $15 prepaid, 30/day smooths spikes.
    { name = "AGENTCORE_PROXY_SECRET_ARN", value = data.aws_secretsmanager_secret.proxy.arn },
    { name = "PROXY_MONTHLY_SESSION_CAP", value = "600" },
    { name = "PROXY_DAILY_SESSION_CAP", value = "30" },
    # Per-user daily live-hunt cap. Free tier is one: the agent-count limit
    # bounds agents, not hunts, and delete-and-recreate mints a fresh first
    # hunt (which always runs live), so without this one account could loop
    # that and spend the global LLM budget. Owner account is exempt.
    { name = "USER_DAILY_HUNT_CAP_FREE", value = "3" },
    { name = "USER_DAILY_HUNT_CAP_PRO", value = "40" },
    { name = "HUNT_CAP_EXEMPT_EMAILS", value = "josephbarbosa416@gmail.com" },
    # Hard monthly cap on browserbase (cancelled 2026-08-16; kept as a guard
    # in case a lane is ever re-pinned there). Nothing pins it now.
    { name = "BROWSERBASE_MONTHLY_SESSION_CAP", value = "500" },
    { name = "LISTING_STALE_DAYS", value = "3" },
    { name = "ALERT_EMAIL_COOLDOWN_H", value = "12" },
  ]

  common_secrets = [
    for k in [
      "DATABASE_URL", "REDIS_URL", "SECRET_KEY",
      "BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID",
      "RESEND_API_KEY", "SENTRY_DSN",
      "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_ID",
    ] : {
      name      = k
      valueFrom = "arn:aws:ssm:us-east-1:${data.aws_caller_identity.current.account_id}:parameter/studeal/prod/${k}"
    }
  ]

  services = {
    api = {
      command = ["uvicorn", "dealbot.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
      cpu     = 256
      memory  = 512
      count   = 2
    }
    worker = {
      # The hunts live here. 4 GB: a hunt is 6 Playwright CDP lanes + chunked
      # extraction; 1 GB OOM-killed the pool (proof hunt 2026-08-14, SIGKILL).
      # Concurrency pinned to the fleet's hunt cap so forks stay bounded.
      command = ["celery", "-A", "dealbot.worker.celery_app", "worker", "--loglevel=info", "--concurrency=4"]
      cpu     = 1024
      memory  = 4096
      count   = 1
    }
    beat = {
      # EXACTLY one, forever: two beats double every cron. Schedule file in
      # /tmp: the image runs non-root so /app is read-only to it, and the
      # file is losable bookkeeping anyway (recomputed on boot).
      command = ["celery", "-A", "dealbot.worker.celery_app", "beat", "--loglevel=info", "--schedule", "/tmp/celerybeat-schedule"]
      cpu     = 256
      memory  = 512
      count   = 1
    }
  }
}

resource "aws_ecs_task_definition" "service" {
  for_each = local.services

  family                   = "studeal-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name        = each.key
    image       = local.image
    command     = each.value.command
    environment = local.common_env
    secrets     = local.common_secrets
    # Only api receives traffic; the ALB attachment requires the port declared.
    portMappings = each.key == "api" ? [{ containerPort = 8000, protocol = "tcp" }] : []
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = "us-east-1"
        awslogs-stream-prefix = each.key
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.service["api"].arn
  desired_count   = local.services.api.count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true    # the no-NAT posture: egress via own public IP
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # New tasks must pass the ALB health check before old ones drain.
  depends_on = [aws_lb_listener.https]
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.service["worker"].arn
  desired_count   = local.services.worker.count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }
}

resource "aws_ecs_service" "beat" {
  name            = "beat"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.service["beat"].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # Never let a deploy briefly run two beats: stop the old one first.
  deployment_maximum_percent         = 100
  deployment_minimum_healthy_percent = 0

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }
}
