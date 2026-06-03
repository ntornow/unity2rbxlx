using UnityEngine;
using UnityEngine.UI;

// Client-domain: drives UI + reads local input, and holds a serialized
// reference to a server-domain NetworkBehaviour (the cross-domain edge).
public class ClientCtl : MonoBehaviour
{
    public ServerCtl target;
    public Text statusLabel;

    void Update()
    {
        if (Input.GetKeyDown(KeyCode.E) && target != null)
        {
            target.CmdActivate();
        }
        if (statusLabel != null)
        {
            statusLabel.text = "ready";
        }
    }
}
