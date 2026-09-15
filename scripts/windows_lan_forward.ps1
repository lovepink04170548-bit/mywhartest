param(
    [string]$ListenAddress = "",
    [int]$ListenPort = 5173,
    [string]$TargetHost = "127.0.0.1",
    [int]$TargetPort = 5173
)

$ErrorActionPreference = "Stop"

function Get-DefaultLanIPv4 {
    $candidate = Get-NetIPConfiguration |
        Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } |
        ForEach-Object { $_.IPv4Address.IPAddress } |
        Where-Object { $_ -and $_ -notlike '127.*' -and $_ -notlike '169.254*' } |
        Select-Object -First 1

    if ($candidate) {
        return $candidate
    }

    $fallback = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254*' } |
        Select-Object -First 1 -ExpandProperty IPAddress

    if ($fallback) {
        return $fallback
    }

    throw "Unable to determine a LAN IPv4 address."
}

if ([string]::IsNullOrWhiteSpace($ListenAddress)) {
    $ListenAddress = Get-DefaultLanIPv4
}

Add-Type -TypeDefinition @"
using System;
using System.Net;
using System.Net.Sockets;
using System.Threading.Tasks;

public static class TcpForwarder
{
    public static void Run(string listenAddress, int listenPort, string targetHost, int targetPort)
    {
        var listener = new TcpListener(IPAddress.Parse(listenAddress), listenPort);
        listener.ExclusiveAddressUse = false;
        listener.Server.ExclusiveAddressUse = false;
        listener.Server.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
        listener.Start();
        Console.WriteLine("Forwarding {0}:{1} -> {2}:{3}", listenAddress, listenPort, targetHost, targetPort);

        while (true)
        {
            var client = listener.AcceptTcpClient();
            Task.Run(() => Handle(client, targetHost, targetPort));
        }
    }

    private static void Handle(TcpClient client, string targetHost, int targetPort)
    {
        using (client)
        using (var target = new TcpClient())
        {
            target.Connect(targetHost, targetPort);

            using (var clientStream = client.GetStream())
            using (var targetStream = target.GetStream())
            {
                var clientToTarget = clientStream.CopyToAsync(targetStream);
                var targetToClient = targetStream.CopyToAsync(clientStream);
                Task.WaitAny(clientToTarget, targetToClient);
            }
        }
    }
}
"@

[TcpForwarder]::Run($ListenAddress, $ListenPort, $TargetHost, $TargetPort)
