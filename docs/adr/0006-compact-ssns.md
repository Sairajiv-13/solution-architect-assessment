# 0006. Redact compact SSNs

Redact an uninterrupted run of nine ASCII digits as well as SSNs grouped by spaces or hyphens. A chunk boundary does not end a digit run. The scanner waits for the next character or a clean end of stream to distinguish nine digits from a longer number, using its existing buffer.

Nine-digit order IDs and phone fragments have the same shape. Masking those values is acceptable for this output policy because exposing an SSN is more costly than hiding a reference number. Longer uninterrupted runs are outside this rule. They still pass through the other pattern checks, including the Luhn rule for cards.
