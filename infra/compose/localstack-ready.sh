#!/bin/sh
# LocalStack ready.d contract: selected local development bucket, never AWS.
set -eu
bucket="${SA_ALLURE_S3_BUCKET:?configured local bucket required}"
region="${SA_ALLURE_S3_REGION:?configured local region required}"
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_SESSION_TOKEN=
export AWS_EC2_METADATA_DISABLED=true AWS_DEFAULT_REGION="$region"

head_bucket() {
    awslocal --endpoint-url http://127.0.0.1:4566 --region "$region" \
        s3api head-bucket --bucket "$bucket" >/dev/null 2>&1
}
if head_bucket; then
    exit 0
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
head_bucket
