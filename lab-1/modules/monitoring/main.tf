# monitoring/main.tf
#
# CloudWatch alarms and SNS alerting for CyberRange.
#
# Resources created:
# - SNS topic for alerts (email subscription)
# - CloudWatch alarm: worker heartbeat stale (per worker)
# - CloudWatch alarm: API 5xx error rate
# - CloudWatch alarm: /health/ready endpoint failures
# - CloudWatch dashboard: unified ops view
#
# Usage:
#   cd monitoring
#   terraform init
#   terraform apply \
#     -var="alert_email=ops@yourorg.com" \
#     -var="api_log_group=/cyberrange/api"

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Use the same S3 backend as the lab modules
  backend "s3" {
    bucket         = "cyberrange-tfstate-prod-ap-south-1"
    key            = "monitoring/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "cyberrange-tfstate-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
}

# ── Variables ─────────────────────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-south-1"
}

variable "alert_email" {
  description = "Email address to receive CloudWatch alarm notifications"
  type        = string
}

variable "api_log_group" {
  description = "CloudWatch log group name for the CyberRange API (stdout → journal → CloudWatch agent)"
  type        = string
  default     = "/cyberrange/api"
}

variable "worker_heartbeat_stale_threshold_s" {
  description = "Seconds after which a worker heartbeat is considered stale"
  type        = number
  default     = 60
}

variable "environment" {
  description = "Environment tag"
  type        = string
  default     = "prod"
}

# ── SNS topic ─────────────────────────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  name = "cyberrange-alerts-${var.environment}"

  tags = {
    Project     = "CyberRange"
    Environment = var.environment
  }
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ── Worker heartbeat alarms (one per worker) ──────────────────────────────────
# Fires when WorkerHeartbeatAge > threshold for 2 consecutive 1-minute periods.
# The metric is published by backend/utils/cloudwatch.py every 30 seconds.

locals {
  worker_ids = ["lab_worker", "lab_cleanup_worker"]
}

resource "aws_cloudwatch_metric_alarm" "worker_heartbeat" {
  for_each = toset(local.worker_ids)

  alarm_name          = "cyberrange-worker-stale-${each.value}-${var.environment}"
  alarm_description   = "Worker ${each.value} has not sent a heartbeat for over ${var.worker_heartbeat_stale_threshold_s}s"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.worker_heartbeat_stale_threshold_s
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  period              = 60
  statistic           = "Maximum"
  treat_missing_data  = "breaching"  # missing metric = worker is down

  namespace   = "CyberRange/Workers"
  metric_name = "WorkerHeartbeatAge"

  dimensions = {
    WorkerId = each.value
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project     = "CyberRange"
    Environment = var.environment
    Worker      = each.value
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = var.api_log_group
  retention_in_days = 30

  tags = {
    Project     = "CyberRange"
    Environment = var.environment
  }
}

# ── API 5xx error rate alarm ──────────────────────────────────────────────────
# Requires the CloudWatch agent to be shipping API logs.
# Metric filter counts log lines containing "HTTP 5" (uvicorn access log format).

resource "aws_cloudwatch_log_metric_filter" "api_5xx" {
  name           = "cyberrange-api-5xx-${var.environment}"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "{ $.status >= 500 }"

  metric_transformation {
    name          = "Api5xxCount"
    namespace     = "CyberRange/API"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "api_5xx_rate" {
  alarm_name          = "cyberrange-api-5xx-rate-${var.environment}"
  alarm_description   = "API 5xx error count exceeded 10 in a 5-minute window"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 10
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  period              = 300
  statistic           = "Sum"
  treat_missing_data  = "notBreaching"

  namespace   = "CyberRange/API"
  metric_name = "Api5xxCount"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project     = "CyberRange"
    Environment = var.environment
  }
}

# ── Terraform provisioning failure alarm ──────────────────────────────────────
# Counts log lines where Terraform apply exited non-zero.

resource "aws_cloudwatch_log_metric_filter" "terraform_failure" {
  name           = "cyberrange-terraform-failure-${var.environment}"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "{ $.message = \"*Terraform apply failed*\" }"

  metric_transformation {
    name          = "TerraformFailureCount"
    namespace     = "CyberRange/Workers"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "terraform_failures" {
  alarm_name          = "cyberrange-terraform-failures-${var.environment}"
  alarm_description   = "Multiple Terraform provisioning failures detected"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 3
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  period              = 300
  statistic           = "Sum"
  treat_missing_data  = "notBreaching"

  namespace   = "CyberRange/Workers"
  metric_name = "TerraformFailureCount"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project     = "CyberRange"
    Environment = var.environment
  }
}

# ── CloudWatch Dashboard ──────────────────────────────────────────────────────

resource "aws_cloudwatch_dashboard" "cyberrange" {
  dashboard_name = "CyberRange-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric"
        x = 0 
        y = 0 
        width = 12 
        height = 6
        properties = {
          title  = "Worker Heartbeat Age (seconds)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            for wid in local.worker_ids :
            ["CyberRange/Workers", "WorkerHeartbeatAge", "WorkerId", wid]
          ]
          yAxis = { left = { min = 0, max = 120 } }
          annotations = {
            horizontal = [{
              label = "Stale threshold"
              value = var.worker_heartbeat_stale_threshold_s
              color = "#ff6961"
            }]
          }
          period = 30
          stat   = "Maximum"
        }
      },
      {
        type = "metric"
        x = 12
        y = 0
        width = 12
        height = 6
        properties = {
          title  = "API 5xx Errors (5-min sum)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [["CyberRange/API", "Api5xxCount"]]
          period = 300
          stat   = "Sum"
        }
      },
      {
        type = "metric"
        x = 0
        y = 6
        width = 12
        height = 6
        properties = {
          title  = "Terraform Provisioning Failures (5-min sum)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [["CyberRange/Workers", "TerraformFailureCount"]]
          period = 300
          stat   = "Sum"
        }
      },
      {
        type = "alarm"
        x = 12
        y = 6
        width = 12
        height = 6
        properties = {
          title  = "Active Alarms"
          alarms = concat(
            [for wid in local.worker_ids :
              "arn:aws:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:cyberrange-worker-stale-${wid}-${var.environment}"
            ],
            [
              "arn:aws:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:cyberrange-api-5xx-rate-${var.environment}",
              "arn:aws:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:cyberrange-terraform-failures-${var.environment}",
            ]
          )
        }
      }
    ]
  })
}

data "aws_caller_identity" "current" {}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "sns_topic_arn" {
  description = "SNS topic ARN for CyberRange alerts"
  value       = aws_sns_topic.alerts.arn
}

output "dashboard_url" {
  description = "CloudWatch dashboard URL"
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.cyberrange.dashboard_name}"
}