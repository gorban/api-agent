#!/bin/sh
# LocalStack ready.d contract: every selected local development bucket, never AWS.
# SA_S3_BUCKETS is a comma-separated list. The container itself carries only the
# region (drift there is refused); the launcher passes the buckets when it starts
# the container and again on every acquire so a missing bucket is created on a
# running container. Existing buckets are left untouched; an empty list is valid.
set -eu
region="${SA_S3_REGION:?configured local region required}"
buckets="${SA_S3_BUCKETS:-}"
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_SESSION_TOKEN=
export AWS_EC2_METADATA_DISABLED=true AWS_DEFAULT_REGION="$region"

head_bucket() {
    awslocal --endpoint-url http://127.0.0.1:4566 --region "$region" \
        s3api head-bucket --bucket "$1" >/dev/null 2>&1
}
set -f
IFS=,
for bucket in $buckets; do
    [ -n "$bucket" ] || continue
    if head_bucket "$bucket"; then
        continue
    fi
    if [ "$region" = us-east-1 ]; then
        awslocal --endpoint-url http://127.0.0.1:4566 --region "$region" \
            s3api create-bucket --bucket "$bucket" >/dev/null
    else
        awslocal --endpoint-url http://127.0.0.1:4566 --region "$region" \
            s3api create-bucket --bucket "$bucket" \
            --create-bucket-configuration "LocationConstraint=$region" >/dev/null
    fi
    # Creation errors propagate, and a successful create must actually be readable.
    head_bucket "$bucket"
done
