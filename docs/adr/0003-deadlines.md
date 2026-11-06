# 0003. Give the primary 3,000 ms

The assessment sets the primary deadline at three seconds. Apply it to the entire attempt so a stream of tiny upstream chunks cannot reset the clock. The backup has its own five-second default. Retry only a primary timeout or HTTP 429, and only before the client sees text.

The timeout cancels local I/O. It cannot cancel remote billing. A stream that exceeds its budget after output starts ends with an error event.
