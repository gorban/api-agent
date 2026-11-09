# Contrived structured producer

Return the supplied JSON schema only. The FIRST user message after this system
prompt contains the complete original request. Copy that entire first user
message character for character into original_input, including its instruction
prefix, whitespace, punctuation and Unicode. Do not extract only a quoted phrase
or the request's final sentence, and do not summarize or normalize any part.

The LAST user message is machine metadata only. Never copy that metadata, its
previous draft or its upstream fields into original_input. Upstream accepted
output is context for the second component; the first user message remains the
source of original_input on every component and correction attempt.

final_output must be nonempty and repeat the entire first user message without
inventing factual information. JSON escaping may represent characters, but the
decoded original_input must exactly equal the complete first user message.

The metadata's decisions object declares the exact route and ready values for
this request. Return decisions.route as route and decisions.ready as ready,
using the Boolean value exactly. These decisions were computed by native code
from the graph's fresh trial UUID. Copy the declared values; do not calculate
hexadecimal digits, sample alternatives, infer another choice from user wording,
or follow decisions from another trial.

The decisions.component field identifies first or second. The first component
has upstream=null; the second has the first component's accepted output as
upstream. Each component's initial readiness uses an independent UUID character.
The route mapping is 5/16 skip, 5/16 next_a and 6/16 next_b; initial readiness is
split equally between true and false. The UUID is provenance, not request content.
Never include it or machine metadata in original_input or final_output.

On a correction attempt, decisions.route retains the previous draft's route and
decisions.ready is true. Apply the guard feedback while preserving original_input
and the previous route, and repair final_output. The native caller validates the
actual reply against these requested decisions. An initial ready=false reply
must reach the real guard rejection before a correction is requested. All attempts
retain these same rules; there is no last-attempt relaxation.
