using UnityEngine;
using Mirror;

// Server-domain: a Mirror NetworkBehaviour with a [Command] + [SyncVar].
public class ServerCtl : NetworkBehaviour
{
    [SyncVar] public bool activated;

    [Command]
    public void CmdActivate()
    {
        activated = true;
    }
}
