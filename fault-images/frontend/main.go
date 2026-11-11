// A "bad release" of the Online Boutique frontend.
//
// It runs the real frontend on an internal port and sits in front of it,
// answering a fixed share of requests with HTTP 500. Health checks always
// pass, so the pod looks healthy to Kubernetes while users see errors, which
// is what a broken release looks like.
//
// The failure rate is compiled into the image (see the Dockerfile), not read
// from the Deployment, so the only difference from a good release is the
// image itself.
package main

import (
	"log"
	"math/rand/v2"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"strconv"
)

// Set at build time with -ldflags "-X main.failPercent=30".
var failPercent = "30"

const upstreamPort = "8081"

func main() {
	log.SetFlags(0) // the frontend logs plain JSON lines; match it
	percent, err := strconv.Atoi(failPercent)
	if err != nil || percent < 0 || percent > 100 {
		log.Fatalf("invalid failure percentage %q", failPercent)
	}
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	server := exec.Command("/src/server")
	server.Env = append(os.Environ(), "PORT="+upstreamPort)
	server.Stdout, server.Stderr = os.Stdout, os.Stderr
	if err := server.Start(); err != nil {
		log.Fatalf("starting frontend: %v", err)
	}
	go func() {
		// If the real frontend exits, exit too so the container restarts.
		err := server.Wait()
		log.Fatalf("frontend exited: %v", err)
	}()

	target, _ := url.Parse("http://127.0.0.1:" + upstreamPort)
	proxy := httputil.NewSingleHostReverseProxy(target)

	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/_healthz" && rand.IntN(100) < percent {
			// Written like an application error, without naming the cause.
			log.Printf(`{"severity":"error","http.req.path":%q,"message":"request error: failed to render page: runtime error: invalid memory address or nil pointer dereference"}`, r.URL.Path)
			http.Error(w, "Internal Server Error", http.StatusInternalServerError)
			return
		}
		proxy.ServeHTTP(w, r)
	})
	log.Fatal(http.ListenAndServe(":"+port, nil))
}
