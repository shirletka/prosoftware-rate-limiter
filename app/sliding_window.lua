-- Sliding window log: one sorted set per API key, score = request time in ms.
-- KEYS[1] = bucket key
-- ARGV[1] = now (ms), ARGV[2] = window (ms), ARGV[3] = limit, ARGV[4] = unique request id
-- Returns {allowed (0/1), requests in window, timestamp of the request whose expiry frees the next slot}

local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

-- The window is (now - window, now]: a request made exactly `window` ms ago no longer counts.
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

local count = redis.call('ZCARD', key)
local allowed = 0
if count < limit then
  redis.call('ZADD', key, now, ARGV[4])
  redis.call('PEXPIRE', key, window)
  count = count + 1
  allowed = 1
end

-- The next slot frees up when the oldest request leaves the window. If the window holds more than
-- `limit` requests (the limit was lowered on redeploy), count - limit + 1 of them have to leave first.
local index = math.max(count - limit, 0)
local blocking = redis.call('ZRANGE', key, index, index, 'WITHSCORES')
local blocking_ts = now
if blocking[2] then
  blocking_ts = tonumber(blocking[2])
end

return {allowed, count, blocking_ts}
