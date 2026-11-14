// M1-00 toolchain smoke test: goroutines, sha256, std only.
package main

import (
	"crypto/sha256"
	"fmt"
	"sync"
)

func main() {
	var wg sync.WaitGroup
	var mu sync.Mutex
	sum := 0
	for i := 1; i <= 10; i++ {
		wg.Add(1)
		go func(n int) { defer wg.Done(); mu.Lock(); sum += n; mu.Unlock() }(i)
	}
	wg.Wait()
	if sum != 55 {
		panic("bad sum")
	}
	fmt.Printf("go-smoke ok sum=%d sha256(writ)=%x\n", sum, sha256.Sum256([]byte("writ")))
}
