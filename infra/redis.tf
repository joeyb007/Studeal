# Managed Redis: one small node. Everything stored here is losable by
# design (queues, counters, locks — the app fails open), so no replica.

resource "aws_elasticache_subnet_group" "main" {
  name       = "studeal"
  subnet_ids = aws_subnet.public[*].id
}

resource "aws_elasticache_cluster" "main" {
  cluster_id      = "studeal"
  engine          = "redis"
  engine_version  = "7.1"
  node_type       = "cache.t4g.micro"
  num_cache_nodes = 1

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]
}

resource "aws_ssm_parameter" "redis_url" {
  name  = "/studeal/prod/REDIS_URL"
  type  = "SecureString"
  value = "redis://${aws_elasticache_cluster.main.cache_nodes[0].address}:6379/0"
}

output "redis_endpoint" {
  value = aws_elasticache_cluster.main.cache_nodes[0].address
}
