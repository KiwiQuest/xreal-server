// XREAL Eye – RGB test v Unity (Windows, BEZ XREAL SDK).
// XREAL SDK na Windows k bryli nesahá (jen Android), proto se tu pouziva
// obycejne kamerove API Windows pres WebCamTexture.
//
// Pouziti: prazdna scena -> prazdny GameObject -> pridej tento skript -> Play.
// Vlevo seznam kamer (kliknutim spustis), vpravo nahled + rozliseni, fps, barevnost.
// "Ulozit snimek" ulozi PNG vedle slozky Assets.
using System.IO;
using System.Text;
using UnityEngine;

public class EyeWebcamProbe : MonoBehaviour
{
    public int requestWidth = 1920;
    public int requestHeight = 1080;
    public int requestFps = 30;

    WebCamDevice[] devices;
    WebCamTexture tex;
    int current = -1;
    int frames;
    float timer, measuredFps, colorScore, brightness;
    string status = "Vyber kameru vlevo.";
    Vector2 scroll;

    void Start()
    {
        devices = WebCamTexture.devices;
        var sb = new StringBuilder("[EyeWebcamProbe] kamery:\n");
        for (int i = 0; i < devices.Length; i++)
            sb.AppendLine($"  [{i}] {devices[i].name}  front={devices[i].isFrontFacing}");
        Debug.Log(sb.ToString());
    }

    void Play(int i)
    {
        if (tex != null) { tex.Stop(); Destroy(tex); }
        current = i;
        tex = new WebCamTexture(devices[i].name, requestWidth, requestHeight, requestFps);
        tex.Play();
        frames = 0; timer = 0; measuredFps = 0; colorScore = 0; brightness = 0;
        status = "Spoustim " + devices[i].name + " …";
    }

    void Update()
    {
        if (tex == null || !tex.isPlaying) return;
        if (tex.didUpdateThisFrame) frames++;
        timer += Time.unscaledDeltaTime;
        if (timer < 1f) return;

        measuredFps = frames / timer;
        frames = 0; timer = 0;
        if (tex.width <= 16) { status = "Zatim zadny obraz…"; return; }

        // barevnost: prumerny rozdil kanalu (0 = sede, > ~4 = barevne)
        Color32[] px = tex.GetPixels32();
        long diff = 0, sum = 0; int n = 0;
        for (int k = 0; k < px.Length; k += 64)
        {
            var c = px[k];
            diff += Mathf.Abs(c.r - c.g) + Mathf.Abs(c.g - c.b);
            sum += c.r + c.g + c.b;
            n++;
        }
        colorScore = n > 0 ? diff / (2f * n) : 0;
        brightness = n > 0 ? sum / (3f * n) : 0;
        string verdict = colorScore > 4f ? "BAREVNY obraz" : "SEDY obraz";
        status = $"{tex.width}x{tex.height}  ~{measuredFps:F1} fps  jas {brightness:F0}  barevnost {colorScore:F1} -> {verdict}";
        Debug.Log($"[EyeWebcamProbe] [{current}] {devices[current].name}: {status}");
    }

    void SaveSnapshot()
    {
        if (tex == null || tex.width <= 16) return;
        var t2 = new Texture2D(tex.width, tex.height, TextureFormat.RGB24, false);
        t2.SetPixels32(tex.GetPixels32());
        t2.Apply();
        string path = Path.Combine(Application.dataPath, "..",
            $"EyeSnap_{current}_{tex.width}x{tex.height}_{System.DateTime.Now:HHmmss}.png");
        File.WriteAllBytes(path, t2.EncodeToPNG());
        Destroy(t2);
        status += "\nUlozeno: " + Path.GetFullPath(path);
        Debug.Log("[EyeWebcamProbe] ulozeno " + Path.GetFullPath(path));
    }

    void OnGUI()
    {
        GUILayout.BeginArea(new Rect(10, 10, 360, Screen.height - 20));
        GUILayout.Label($"Kamery ve Windows: {devices?.Length ?? 0}");
        scroll = GUILayout.BeginScrollView(scroll);
        if (devices != null)
            for (int i = 0; i < devices.Length; i++)
                if (GUILayout.Button($"[{i}] {devices[i].name}" + (i == current ? "  ◀" : "")))
                    Play(i);
        GUILayout.EndScrollView();
        if (GUILayout.Button("Ulozit snimek")) SaveSnapshot();
        if (GUILayout.Button("Znovu nacist seznam")) Start();
        GUILayout.Label(status);
        GUILayout.EndArea();

        if (tex != null && tex.width > 16)
        {
            float x = 380, w = Screen.width - x - 10;
            float h = w * tex.height / tex.width;
            if (h > Screen.height - 20) { h = Screen.height - 20; w = h * tex.width / tex.height; }
            GUI.DrawTexture(new Rect(x, 10, w, h), tex, ScaleMode.ScaleToFit);
        }
    }

    void OnDestroy()
    {
        if (tex != null) tex.Stop();
    }
}
