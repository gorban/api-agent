# Mantle list-price estimates

[Provider setup](README.md)

The reviewed snapshot qualifies us-east-1 Mantle Responses or Chat Completions,
Standard tier only:

| Model | USD per million input tokens | USD per million output tokens |
| --- | --- | --- |
| openai.gpt-oss-20b | 0.07 | 0.30 |
| openai.gpt-oss-120b | 0.15 | 0.60 |

The [public AmazonBedrock offer](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/20261016210806/us-east-1/index.json)
version 20261016210806 supplies these rates. The OSS120B Standard Mantle input
and output SKUs are K7EPD38WB9WRZSJH and UEJ6RRGVWV8HT4KC, respectively;
Safeguard products are excluded. Other models, regions or tiers remain explicitly
unpriced.

Quotes freeze the AWS source/revision and token usage; compatible history can be
repriced for picker estimates. Unknown cache/reasoning subdivisions remain unknown.
Both models’ published two-category schedules price inclusive input/output totals,
without counting reasoning twice or claiming a zero cache count. These are list
estimates, excluding account credits/discounts and any unqualified ancillary
charges. No billing permission is needed to use the bundled public rate snapshot.
Displaying configuration or predictions does not authenticate or fetch prices.

AWS billing reconciliation through Cost Explorer or CUR is separate future work;
this provider does not call those APIs or report exact invoice charges. See the
[cost requirements](../../../../docs/prds/implementation_plan_f4k.md).
