# ECS: cluster, the two IAM roles, logs, and task definitions.
#
# The two-role split that trips everyone up:
#   EXECUTION role — used by Fargate's machinery BEFORE your code runs:
#     pull the image from ECR, fetch SSM secrets, open the log stream.
#   TASK role — assumed by YOUR CODE at runtime: Bedrock calls. This is why
#     prod needs no AWS keys in env — boto3 finds these credentials
#     automatically via the task metadata endpoint.

resource "aws_ecs_cluster" "main" {
  name = "studeal"
}

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "studeal-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_base" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# SecureString params are KMS-encrypted under the account's default SSM key;
# fetching them at container start needs both permissions.
resource "aws_iam_role_policy" "execution_secrets" {
  name = "read-studeal-params"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameters"]
        Resource = "arn:aws:ssm:us-east-1:${data.aws_caller_identity.current.account_id}:parameter/studeal/prod/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.us-east-1.amazonaws.com" }
        }
      }
    ]
  })
}

resource "aws_iam_role" "task" {
  name               = "studeal-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "task_bedrock" {
  name = "bedrock-invoke"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        # AgentCore managed browser: the fleet's default backend. Worker
        # tasks start/stop sessions and connect to their CDP endpoints.
        "bedrock-agentcore:StartBrowserSession",
        "bedrock-agentcore:StopBrowserSession",
        "bedrock-agentcore:GetBrowserSession",
        "bedrock-agentcore:ListBrowserSessions",
        "bedrock-agentcore:UpdateBrowserStream",
        "bedrock-agentcore:ConnectBrowserAutomationStream",
        "bedrock-agentcore:GetBrowser",
      ]
      Resource = "*"
    }]
  })
}

# The residential-proxy Basic-Auth secret ({username,password}), created out
# of band in the console. Looked up by name so its full ARN (with suffix) is
# resolvable for both the IAM grant and the task env.
data "aws_secretsmanager_secret" "proxy" {
  name = "studeal/prod/proxy-credentials"
}

# AgentCore reads those credentials from Secrets Manager when opening a proxied
# browser session (FB lanes). Scoped to the one secret; the DataImpulse
# password never touches env or code.
resource "aws_iam_role_policy" "task_proxy_secret" {
  name = "proxy-secret-read"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = data.aws_secretsmanager_secret.proxy.arn
    }]
  })
}

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "app" {
  name              = "/studeal/prod"
  retention_in_days = 30
}

# ---------------------------------------------------------------------------
# Migration one-off: same image, command = alembic. Run manually via
# `aws ecs run-task` on deploys that carry a migration.
# ---------------------------------------------------------------------------

resource "aws_ecs_task_definition" "migrate" {
  family                   = "studeal-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    cpu_architecture        = "ARM64"    # Graviton: matches the Mac-built image
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name    = "migrate"
    image   = "${aws_ecr_repository.app.repository_url}:v1"
    command = ["alembic", "upgrade", "head"]
    secrets = [{
      name      = "DATABASE_URL"
      valueFrom = aws_ssm_parameter.database_url.arn
    }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = "us-east-1"
        awslogs-stream-prefix = "migrate"
      }
    }
  }])
}
