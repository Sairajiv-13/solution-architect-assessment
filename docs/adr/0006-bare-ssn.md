# 0006. Redact standalone nine-digit runs as SSNs

A nine-digit run with no digit on either side is redacted as an SSN. SSNs have no checksum, so unlike cards there is no test that separates a real SSN from an order or account number of the same length. A ten-digit phone number is untouched because the match needs a non-digit on both sides, and longer digit runs are untouched for the same reason.

This trades false positives for coverage: an order number that happens to be nine digits is redacted. In this gateway the cost of leaking an SSN is higher than the cost of masking an order number, so the rule fails toward redaction. Grouped SSNs stay on the separator rule.
