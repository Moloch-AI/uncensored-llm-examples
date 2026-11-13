# Rate limiting: four common algorithms and their trade-offs

Rate limiting protects a service from being overwhelmed, shares capacity fairly between clients, and keeps costs predictable. Almost every public API applies some form of it. The algorithm behind a limit decides how bursts are treated, how much memory each client costs, and how a client should pace itself. This note compares four common designs: fixed window, sliding log, sliding window counter, and token bucket, with leaky bucket as a close relative of the last.

## Fixed window

A fixed window counter divides time into intervals of equal length, for example one minute, and counts requests per client in the current interval. When the count reaches the limit, further requests are rejected until the next interval begins. The state per client is a single integer and a timestamp, which makes it cheap to store in memory or in a shared cache.

The weakness is the boundary. A client can send the full allowance in the last second of one window and the full allowance again in the first second of the next, so in a two-second span it gets twice the advertised rate. For limits that exist to protect a backend from load spikes, that burst can matter. For limits that exist to divide a monthly quota, it usually does not.

## Sliding log

A sliding log stores the timestamp of every accepted request. To decide on a new request, the limiter drops timestamps older than the window length and counts what remains. If the count is below the limit, the request is accepted and its timestamp is added.

The sliding log is exact: at no point in time does any window of the configured length contain more than the limit. The cost is memory proportional to the limit, per client, and the work of pruning old entries. With a limit of sixty requests per minute that is cheap. With limits in the tens of thousands per client it becomes expensive, especially in a shared store where every check is a network round trip.

A useful property for clients is that a sliding log can tell a rejected client exactly when the next slot frees up: it is the moment the oldest timestamp in the window expires. Servers often send this as a `Retry-After` header.

## Sliding window counter

The sliding window counter approximates the sliding log with two fixed-window counters: the current window and the previous one. The estimated count is the current count plus the previous count weighted by how much of the previous window still overlaps the sliding window. If a minute-long window is a quarter of the way through, the estimate is the current count plus three quarters of the previous count.

This keeps the memory of a fixed window while removing most of the boundary burst. The estimate assumes requests in the previous window were evenly spread, so it can be slightly too strict or too lenient, but in practice the error is small and the approach is popular in shared caches.

## Token bucket

A token bucket holds up to a fixed number of tokens and refills at a steady rate. Each request takes one token; a request that finds the bucket empty is rejected or delayed. The bucket size sets the largest burst a client may send at once, and the refill rate sets the long-term average.

Token buckets separate two questions that the window algorithms mix together: how bursty a client may be and how fast it may go on average. That makes them a natural fit for client-side pacing as well. A batch job can keep its own bucket with a slightly lower rate than the server's limit and never see a rejection under normal conditions.

The leaky bucket is the mirror image: requests enter a queue that drains at a fixed rate, and requests that arrive when the queue is full are dropped. It smooths output perfectly but adds latency, since accepted requests wait in the queue.

## What clients should do

Whatever the server uses, a well-behaved client follows the same rules. Pace requests below the documented limit rather than discovering the limit through errors. When the server rejects a request for exceeding a rate limit, wait for the time it gives in `Retry-After` before trying again, and cap that wait so a misconfigured header cannot stall the client forever. Distinguish rate limits, which clear within seconds or minutes, from quotas, which clear at a fixed reset time; retrying a quota error before its reset time only wastes requests. Finally, make sure retries happen in one layer only. An SDK that retries three times inside an application loop that also retries three times turns one failure into nine requests.

## Choosing an algorithm

For protecting a single service from overload, a token bucket per client is a good default. For an exact per-minute promise that clients can reason about, a sliding log is simple and precise at small limits. For very large numbers of clients in a shared cache, the sliding window counter is the usual compromise. Fixed windows remain fine for coarse quotas measured in days or months, where a burst at the boundary is irrelevant.
