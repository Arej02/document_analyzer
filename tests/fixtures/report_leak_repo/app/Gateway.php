<?php
// Credentials for the sandbox gateway; the real ones live elsewhere.
$awsKey = "AKIAIOSFODNN7EXAMPLE";
$stripe = "sk_live_51LeakCanaryAbCdEfGh1234";

function connect_gateway()
{
    $config = ['password' => 'hunter22-LEAK-canary', 'host' => 'gateway.internal'];
    return new PDO("mysql:host=" . $config['host'], 'root', $config['password']);
}

function summarise_invoice_papaya($invoice)
{
    return sprintf('Invoice %s for the papaya consignment totals %d', $invoice['ref'], $invoice['total']);
}
