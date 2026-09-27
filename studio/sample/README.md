Tutorial dataset: 50 short support tickets with the queue each should be routed to
(billing, shipping, refund, bug, account, other). Routing rules the tutorial's root prompt does not know:
a request for money back is `refund` whatever caused it; double charges and wrong amounts are `billing`;
subscription changes, login and deletion are `account`; things that do not work are `bug`; praise, feature
requests and general questions are `other`. ~30% of rows are deliberately ambiguous between two queues.

`amounts.jsonl` (the loop example): 14 billing messages and the amount the customer actually owes. About half
state the total outright; the rest hide it behind a discount, credit, deposit, per-item fee or superseded figure,
which is where a first extraction goes wrong and the check prompt should send it back. `feedback` is empty on
every row: it is the first-visit value of the placeholder the loop-back edge fills.
